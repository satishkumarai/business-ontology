"""End-to-end governance tests against a deployed + seeded environment.

Run:  python -m pytest tests/integration -v  (env: ONTOLOGY_ENV=dev)
Requires scripts/seed_demo.py to have run. Each run works in a fresh
QA_<timestamp> domain and cleans it up afterwards.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import deploy  # noqa: E402
import personas  # noqa: E402
from actors import Harness  # noqa: E402

ENV = deploy.load_env(os.environ.get("ONTOLOGY_ENV", "dev"))
DB = ENV["DB"]
QA = f"QA{int(time.time())}"


def fails_with(actor, code, sql, params=None):
    with pytest.raises(Exception) as e:
        actor.call(sql, params)
    assert code in str(e.value), str(e.value)[:400]


@pytest.fixture(scope="session")
def h():
    conn = deploy.connect(type("A", (), {"connection": os.environ.get("ONTOLOGY_CONNECTION")})())
    harness = Harness(conn, ENV)
    cur = harness.admin_cursor()
    cur.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
    cur.execute(f"USE WAREHOUSE {ENV['WAREHOUSE']}")
    for path in (QA, f"{QA}.Child"):
        cur.execute(f"CALL {DB}.API.CREATE_DOMAIN(%s, 'Integration test domain', NULL, 'ONT_DEMO_FIN_STEWARD_ROLE')", (path,))
        for role, priv in (("ONT_DEMO_AUTHOR_ROLE", "SUGGEST"), ("ONT_DEMO_EXEC_ROLE", "READ")):
            cur.execute(f"CALL {DB}.API.SET_DOMAIN_PRIVILEGE(%s, %s, %s, 'GRANT')", (path, priv, role))
    yield harness
    # cleanup: retire everything live in QA domains, then drop them
    cur = harness.admin_cursor()
    cur.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
    st = harness.actor("FIN_STEWARD")
    for (qn,) in cur.execute(f"""SELECT C.QUALIFIED_NAME FROM {DB}.CORE.CONCEPT C JOIN {DB}.CORE.DOMAIN D USING (DOMAIN_ID)
                                  WHERE STARTSWITH(D.PATH, %s) AND C.STATUS IN ('DRAFT','APPROVED')""", (QA,)).fetchall():
        st.call(f"CALL {DB}.API.RETIRE_CONCEPT(%s, 'integration test cleanup')", (qn,))
    cur = harness.admin_cursor()
    cur.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
    cur.execute(f"DROP SEMANTIC VIEW IF EXISTS {DB}.DEMO.QA_DRIFT_SV")
    for path in (f"{QA}.Child", QA):
        cur.execute(f"CALL {DB}.API.DROP_DOMAIN(%s)", (path,))
    harness.close()


def propose(actor, name, kind="METRIC", domain=None, **kw):
    return actor.call(
        f"CALL {DB}.API.PROPOSE_CONCEPT(%s, %s, %s, %s, PARSE_JSON(%s)::ARRAY, %s, PARSE_JSON(%s)::ARRAY, NULL, %s)",
        (domain or QA, name, kind, kw.get("definition", f"Definition of {name} for integration testing."),
         json.dumps(kw.get("synonyms", [])), kw.get("formula"), json.dumps(kw.get("exclusions", [])), kw.get("scope")),
    )


def approve(actor, version_id):
    return actor.call(f"CALL {DB}.API.APPROVE_VERSION(%s, 'ok')", (version_id,))


def visible_domains(actor):
    return {r[0] for r in actor.query(f"SELECT DISTINCT DOMAIN_PATH FROM {DB}.API.V_CONCEPT")}


# ------------------------------------------------------------- visibility
def test_role_scoped_visibility(h):
    assert visible_domains(h.actor("FIN_ANALYST")) == {"Finance"}
    assert {"Finance", "Sales"} <= visible_domains(h.actor("EXEC"))
    assert visible_domains(h.actor("OUTSIDER")) == set()


def test_no_inheritance_to_child_domains(h):
    # Finance READ does not grant Finance.EMEA; steward of both sees both.
    assert "Finance.EMEA" not in visible_domains(h.actor("FIN_ANALYST"))
    assert "Finance.EMEA" in visible_domains(h.actor("FIN_STEWARD"))


def test_drafts_visible_only_to_author_and_steward(h):
    r = propose(h.actor("AUTHOR"), "Draft Visibility Metric")
    q = f"SELECT COUNT(*) FROM {DB}.API.V_REVIEW_QUEUE WHERE ITEM_ID = %s"
    assert h.actor("AUTHOR").query(q, (r["version_id"],))[0][0] == 1
    assert h.actor("FIN_STEWARD").query(q, (r["version_id"],))[0][0] == 1
    assert h.actor("EXEC").query(q, (r["version_id"],))[0][0] == 0
    assert h.actor("EXEC").query(f"SELECT COUNT(*) FROM {DB}.API.V_CONCEPT WHERE QUALIFIED_NAME = %s",
                                 (r["qualified_name"],))[0][0] == 0


def test_no_direct_access_to_core(h):
    with pytest.raises(Exception):
        h.actor("FIN_STEWARD").query(f"SELECT COUNT(*) FROM {DB}.CORE.CONCEPT")


# ---------------------------------------------------------- authorization
def test_author_cannot_approve_and_reader_cannot_suggest(h):
    r = propose(h.actor("AUTHOR"), "Authz Metric")
    fails_with(h.actor("AUTHOR"), "NOT_AUTHORIZED", f"CALL {DB}.API.APPROVE_VERSION(%s)", (r["version_id"],))
    fails_with(h.actor("FIN_ANALYST"), "NOT_AUTHORIZED",
               f"CALL {DB}.API.PROPOSE_CONCEPT('Finance', 'Sneaky', 'TERM', 'An unauthorized proposal attempt.')")
    if h.mode == "users":  # in single-identity mode the human user genuinely holds the admin role
        fails_with(h.actor("OUTSIDER"), "NOT_AUTHORIZED", f"CALL {DB}.API.CREATE_DOMAIN('Rogue')")


def test_separation_of_duties_when_enabled(h):
    cur = h.admin_cursor()
    cur.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
    cur.execute(f"UPDATE {DB}.CORE.SETTINGS SET VALUE = TO_VARIANT(TRUE) WHERE KEY = 'REQUIRE_DISTINCT_APPROVER'")
    try:
        st = h.actor("FIN_STEWARD")
        r = propose(st, "SoD Metric")
        fails_with(st, "separation of duties", f"CALL {DB}.API.APPROVE_VERSION(%s)", (r["version_id"],))
    finally:
        cur = h.admin_cursor()
        cur.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
        cur.execute(f"UPDATE {DB}.CORE.SETTINGS SET VALUE = TO_VARIANT({ENV['REQUIRE_DISTINCT_APPROVER']}) "
                    "WHERE KEY = 'REQUIRE_DISTINCT_APPROVER'")


# ------------------------------------------------------------- validation
def test_model_rules_enforced(h):
    a = h.actor("AUTHOR")
    fails_with(a, "VALIDATION_ERROR",
               f"CALL {DB}.API.PROPOSE_CONCEPT(%s, 'Bad Term', 'TERM', 'Terms may not have formulas.', NULL, 'SUM(x)')", (QA,))
    t1, t2 = propose(a, "Rule Term A", "TERM"), propose(a, "Rule Term B", "TERM")
    fails_with(a, "CUSTOM relationships require a label",
               f"CALL {DB}.API.PROPOSE_RELATIONSHIP(%s, 'CUSTOM', %s)", (t1["qualified_name"], t2["qualified_name"]))
    fails_with(a, "merged instead",
               f"CALL {DB}.API.PROPOSE_RELATIONSHIP(%s, 'EQUIVALENT_TO', %s)", (t1["qualified_name"], t2["qualified_name"]))
    a.call(f"CALL {DB}.API.PROPOSE_RELATIONSHIP(%s, 'IS_A', %s)", (t1["qualified_name"], t2["qualified_name"]))
    fails_with(a, "cycle", f"CALL {DB}.API.PROPOSE_RELATIONSHIP(%s, 'IS_A', %s)", (t2["qualified_name"], t1["qualified_name"]))
    fails_with(a, "CONFLICT", f"CALL {DB}.API.PROPOSE_CONCEPT(%s, 'Rule Term A', 'TERM', 'Duplicate name in the same domain.')", (QA,))


def test_same_name_allowed_with_scope_or_other_domain(h):
    a = h.actor("AUTHOR")
    propose(a, "Daily Active Users", scope="ProductA")
    propose(a, "Daily Active Users", scope="ProductB")
    propose(a, "Daily Active Users", domain=f"{QA}.Child")


# ------------------------------------------------------------ edit flow
def test_edit_keeps_approved_version_until_steward_approves(h):
    a, st, reader = h.actor("AUTHOR"), h.actor("FIN_STEWARD"), h.actor("EXEC")
    r = propose(a, "Edit Flow Metric", formula="SUM(a)")
    approve(st, r["version_id"])
    qn = r["qualified_name"]
    e = a.call(f"CALL {DB}.API.PROPOSE_EDIT(%s, PARSE_JSON(%s))", (qn, json.dumps({"formula": "SUM(a) - SUM(b)"})))
    assert e["status"] == "PROPOSED"
    q = f"SELECT VERSION_NO, FORMULA, HAS_PENDING_EDIT FROM {DB}.API.V_CONCEPT WHERE QUALIFIED_NAME = %s"
    assert reader.query(q, (qn,))[0] == (1, "SUM(a)", True)
    fails_with(a, "pending edit", f"CALL {DB}.API.PROPOSE_EDIT(%s, PARSE_JSON('{{\"formula\": \"SUM(c)\"}}'))", (qn,))
    approve(st, e["version_id"])
    assert reader.query(q, (qn,))[0] == (2, "SUM(a) - SUM(b)", False)
    hist = st.query(f"SELECT VERSION_NO, STATUS FROM {DB}.API.V_CONCEPT_HISTORY WHERE QUALIFIED_NAME = %s ORDER BY 1", (qn,))
    assert hist == [(1, "SUPERSEDED"), (2, "APPROVED")]


def test_reject_requires_note(h):
    r = propose(h.actor("AUTHOR"), "Reject Me Metric")
    fails_with(h.actor("FIN_STEWARD"), "note", f"CALL {DB}.API.REJECT_VERSION(%s, NULL)", (r["version_id"],))
    assert h.actor("FIN_STEWARD").call(f"CALL {DB}.API.REJECT_VERSION(%s, 'Duplicate of an existing metric')",
                                       (r["version_id"],))["status"] == "REJECTED"


def test_merge_within_domain_only(h):
    a, st = h.actor("AUTHOR"), h.actor("FIN_STEWARD")
    s = propose(a, "Churn Rate", synonyms=["attrition"])
    d = propose(a, "Customer Churn", synonyms=["logo churn"])
    x = propose(a, "Churn Rate", domain=f"{QA}.Child")
    for r in (s, d, x):
        approve(st, r["version_id"])
    fails_with(st, "within a domain", f"CALL {DB}.API.MERGE_CONCEPTS(%s, %s)", (s["qualified_name"], x["qualified_name"]))
    st.call(f"CALL {DB}.API.MERGE_CONCEPTS(%s, %s, 'same metric')", (s["qualified_name"], d["qualified_name"]))
    syn = json.loads(st.query(f"SELECT SYNONYMS FROM {DB}.API.V_CONCEPT WHERE QUALIFIED_NAME = %s", (s["qualified_name"],))[0][0])
    assert {"attrition", "Customer Churn", "logo churn"} <= set(syn)


# -------------------------------------------------------------- retrieval
def test_search_flags_ambiguity_and_respects_roles(h):
    sql = f"CALL {DB}.API.SEARCH_CONCEPTS('What was net revenue last quarter?')"
    ex = h.actor("EXEC").call(sql)
    names = [r["qualified_name"] for r in ex["results"]]
    assert "Finance.Net Revenue" in names and "Sales.Net Revenue" in names
    assert "net revenue" in ex["ambiguous_terms"]
    fin = next(r for r in ex["results"] if r["qualified_name"] == "Finance.Net Revenue")
    assert fin["implemented_by"][0]["member"] == "INVOICES.NET_REVENUE"
    assert fin["policies"][0]["policy"] == "Finance.Revenue Recognition Policy"
    an = h.actor("FIN_ANALYST").call(sql)
    assert [r["domain"] for r in an["results"]].count("Sales") == 0 and an["ambiguous_terms"] == []
    assert h.actor("OUTSIDER").call(sql)["result_count"] == 0


def test_search_matches_synonyms(h):
    res = h.actor("EXEC").call(f"CALL {DB}.API.SEARCH_CONCEPTS('net bookings')")
    assert res["results"][0]["qualified_name"] == "Sales.Net Revenue"


def test_get_concept_unqualified_returns_candidates(h):
    res = h.actor("EXEC").call(f"CALL {DB}.API.GET_CONCEPT('Net Revenue')")
    assert res["found"] is False and set(res["candidates"]) >= {"Finance.Net Revenue", "Sales.Net Revenue"}
    full = h.actor("EXEC").call(f"CALL {DB}.API.GET_CONCEPT('Finance.Net Revenue')")
    assert full["found"] and "intercompany invoices" in full["exclusions"]


def test_export_is_role_filtered(h):
    doc = h.actor("FIN_ANALYST").call(f"CALL {DB}.API.EXPORT_ONTOLOGY(NULL, 'JSON')")
    assert {d["domain"] for d in doc["documents"]} == {"Finance"}
    ttl = h.actor("EXEC").call(f"CALL {DB}.API.EXPORT_ONTOLOGY('Sales', 'TURTLE')")["content"]
    assert "skos:Concept" in ttl and "Finance" not in ttl


# -------------------------------------------------------------- ingestion
def test_bootstrap_from_semantic_view_creates_drafts(h):
    res = h.actor("AUTHOR").call(
        f"CALL {DB}.API.BOOTSTRAP_FROM_SEMANTIC_VIEW('{DB}.SEMANTIC.FINANCE_REVENUE_SV', %s)", (f"{QA}.Child",))
    assert {f"{QA}.Child.Net Revenue", f"{QA}.Child.Gross Revenue"} <= set(res["created"])
    src = h.actor("FIN_STEWARD").query(
        f"SELECT SOURCE FROM {DB}.API.V_REVIEW_QUEUE WHERE SUBJECT = %s", (f"{QA}.Child.Net Revenue",))
    assert src[0][0] == "SEMANTIC_VIEW_BOOTSTRAP"


def test_cortex_sense_promotion_lands_as_draft(h):
    finding = {"name": "Active Customer", "kind": "TERM", "definition": "Customer with an invoice in the last 90 days.",
               "evidence": {"source": "cortex_sense", "confidence": 0.82}}
    r = h.actor("AUTHOR").call(f"CALL {DB}.API.PROMOTE_CORTEX_SENSE_FINDING(%s, PARSE_JSON(%s))", (QA, json.dumps(finding)))
    assert r["status"] == "DRAFT"
    assert h.actor("FIN_STEWARD").query(f"SELECT SOURCE FROM {DB}.API.V_REVIEW_QUEUE WHERE ITEM_ID = %s",
                                        (r["version_id"],))[0][0] == "CORTEX_SENSE"


def test_import_dry_run_writes_nothing(h, tmp_path):
    a = h.actor("AUTHOR")
    f = tmp_path / "qa.yaml"
    f.write_text(f"domain: {QA}\nconcepts:\n  - name: Dry Run Term\n    kind: TERM\n    definition: Should never be persisted.\n")
    a.put(f.as_posix(), f"@{DB}.API.IMPORT_STAGE/qa/")
    res = a.call(f"CALL {DB}.API.IMPORT_GLOSSARY('@{DB}.API.IMPORT_STAGE/qa/qa.yaml', 'AUTO', NULL, TRUE)")
    assert res["stats"]["concepts"] == 1 and res["dry_run"]
    assert a.query(f"SELECT COUNT(*) FROM {DB}.API.V_REVIEW_QUEUE WHERE SUBJECT = %s", (f"{QA}.Dry Run Term",))[0][0] == 0


# ------------------------------------------------------------------ drift
SV_DDL = """CREATE OR REPLACE SEMANTIC VIEW {db}.DEMO.QA_DRIFT_SV
  TABLES (B AS {db}.DEMO.BOOKINGS PRIMARY KEY (BOOKING_ID))
  FACTS (B.AMT AS LIST_AMOUNT)
  METRICS (B.QA_METRIC AS {expr} COMMENT = 'qa metric')"""


def test_drift_detected_and_resolved(h):
    cur = h.admin_cursor()
    cur.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
    cur.execute(SV_DDL.format(db=DB, expr="SUM(B.AMT)"))
    a, st = h.actor("AUTHOR"), h.actor("FIN_STEWARD")
    r = propose(a, "Drift Metric", formula="SUM(list_amount)")
    approve(st, r["version_id"])
    rep = a.call(f"CALL {DB}.API.PROPOSE_REPRESENTATION(%s, 'IMPLEMENTS', 'SV_METRIC', '{DB}.DEMO.QA_DRIFT_SV', 'B.QA_METRIC')",
                 (r["qualified_name"],))
    st.call(f"CALL {DB}.API.REVIEW_LINK('REPRESENTATION', %s, 'APPROVE')", (rep["rep_id"],))

    def validate():
        c = h.admin_cursor()
        c.execute(f"USE ROLE {ENV['ADMIN_ROLE']}")
        c.execute(f"CALL {DB}.CORE.VALIDATE_REPRESENTATIONS(%s)", (QA,))
        return c

    c = validate()
    c.execute(SV_DDL.format(db=DB, expr="AVG(B.AMT)"))
    validate()
    q = f"SELECT FINDING_ID, FINDING_TYPE FROM {DB}.API.V_DRIFT_OPEN WHERE QUALIFIED_NAME = %s"
    findings = st.query(q, (r["qualified_name"],))
    assert "FORMULA_CHANGED" in {f[1] for f in findings}  # SUM->AVG may also change TYPE
    assert h.actor("EXEC").query(q, (r["qualified_name"],)) == []  # readers don't see drift queue
    st.call(f"CALL {DB}.API.ACKNOWLEDGE_DRIFT(%s, 'ACCEPT', 'AVG is intended')", (findings[0][0],))  # closes all for the link
    assert st.query(q, (r["qualified_name"],)) == []
    c = validate()
    c.execute(f"DROP SEMANTIC VIEW {DB}.DEMO.QA_DRIFT_SV")
    validate()
    assert [f[1] for f in st.query(q, (r["qualified_name"],))] == ["ASSET_MISSING"]


def test_representation_to_missing_asset_rejected(h):
    r = propose(h.actor("AUTHOR"), "Ghost Metric")
    fails_with(h.actor("AUTHOR"), "NOT_FOUND",
               f"CALL {DB}.API.PROPOSE_REPRESENTATION(%s, 'IMPLEMENTS', 'TABLE', '{DB}.DEMO.NO_SUCH_TABLE')", (r["qualified_name"],))


# ------------------------------------------------------------------ audit
def test_audit_trail_visible_to_steward(h):
    n = h.actor("FIN_STEWARD").query(f"SELECT COUNT(*) FROM {DB}.API.V_AUDIT WHERE DOMAIN_PATH = %s", (QA,))[0][0]
    assert n > 5
    assert h.actor("EXEC").query(f"SELECT COUNT(*) FROM {DB}.API.V_AUDIT WHERE DOMAIN_PATH = %s", (QA,))[0][0] == 0
