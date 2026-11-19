"""Behavioural evaluation of the ontology-grounded agent.

Checks the contract from the design:
  * an ambiguous term with several approved meanings triggers a clarification
  * a disambiguated answer cites the qualified concept, policy and a GOVERNED label
  * the number equals the semantic-view metric computed independently
  * a caller who can only see one domain is not asked to disambiguate
Run: python -m pytest tests/agent -v   (slow; calls Cortex)
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import deploy  # noqa: E402
from actors import Harness  # noqa: E402

ENV = deploy.load_env(os.environ.get("ONTOLOGY_ENV", "dev"))
AGENT = f"{ENV['DB']}.AGENTS.ONTOLOGY_AGENT"
QUESTION = "What was net revenue in Q3 2026?"


@pytest.fixture(scope="module")
def h():
    harness = Harness(deploy.connect(type("A", (), {"connection": os.environ.get("ONTOLOGY_CONNECTION")})()), ENV)
    yield harness
    harness.close()


def ask(actor, turns: list[str]) -> list[str]:
    msgs, answers = [], []
    for i, t in enumerate(turns):
        msgs.append({"role": "user", "content": [{"type": "text", "text": t}]})
        r = actor.call("SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s)", (AGENT, json.dumps({"messages": msgs})))
        r = json.loads(r) if isinstance(r, str) else r
        text = "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        answers.append(text)
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    return answers


def _money(text: str) -> float | None:
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def test_ambiguity_then_governed_answer(h):
    ex = h.actor("EXEC")
    first, second = ask(ex, [QUESTION, "Finance."])
    assert "Finance.Net Revenue" in first and "Sales.Net Revenue" in first, first
    assert "?" in first
    assert "Finance.Net Revenue" in second and "GOVERNED" in second.upper(), second
    assert "Revenue Recognition Policy" in second
    expected = ex.query(
        f"""SELECT SUM(INVOICE_AMOUNT) - SUM(REFUND_AMOUNT) - SUM(DISCOUNT_AMOUNT)
              FROM {ENV['DB']}.DEMO.V_REPORTABLE_INVOICES
             WHERE RECOGNIZED_DATE BETWEEN '2026-07-01' AND '2026-09-30'""")[0][0]
    got = _money(second)
    assert got is not None and abs(got - float(expected)) < 1.0, (got, expected)


def test_single_domain_caller_is_not_asked(h):
    (answer,) = ask(h.actor("FIN_ANALYST"), [QUESTION])
    assert "Sales.Net Revenue" not in answer
    assert "Finance.Net Revenue" in answer
