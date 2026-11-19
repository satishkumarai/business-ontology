"""Unit tests for pure validation rules (no Snowflake required)."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src" / "python"))

from ontology import importer, validation as v  # noqa: E402
from ontology.repo import _bind  # noqa: E402


# ---------------------------------------------------------------- names
def test_qualified_name_roundtrip():
    qn = v.qualified_name("Finance.Revenue", "Net Revenue", "EMEA")
    assert qn == "Finance.Revenue.Net Revenue [EMEA]"
    assert v.parse_qualified_name(qn) == ("Finance.Revenue", "Net Revenue", "EMEA")
    assert v.parse_qualified_name("Finance.Net Revenue") == ("Finance", "Net Revenue", None)


@pytest.mark.parametrize("bad", ["", "1Finance", "Fin..ance", "Fin;DROP"])
def test_bad_domain_paths(bad):
    with pytest.raises(v.ValidationError):
        v.parse_domain_path(bad)


def test_role_name_is_deterministic_and_safe():
    assert v.role_name_for("Finance.Revenue.EMEA", "modify") == "ONT_FINANCE_REVENUE_EMEA_MODIFY"
    assert v.role_name_for("Sales & Ops", "READ", "ONTP") == "ONTP_SALES_OPS_READ"
    with pytest.raises(v.ValidationError):
        v.role_name_for("Finance", "OWNERSHIP")


def test_role_name_rejects_injection():
    with pytest.raises(v.ValidationError):
        v.validate_role_name("X; DROP DATABASE Y")


# ------------------------------------------------------------- concepts
def test_only_metrics_carry_formula():
    v.validate_concept("Net Revenue", "metric", "Recognized revenue after refunds.", formula="SUM(x)", exclusions=["test"])
    for field in ({"formula": "SUM(x)"}, {"exclusions": ["a"]}, {"formula_provenance": "doc"}):
        with pytest.raises(v.ValidationError, match="only"):
            v.validate_concept("Customer", "ENTITY", "A billed legal entity.", **field)


def test_definition_required_and_kind_checked():
    with pytest.raises(v.ValidationError):
        v.validate_concept("X", "TERM", "short")
    with pytest.raises(v.ValidationError):
        v.validate_concept("X", "WIDGET", "A long enough definition.")


def test_synonyms_deduped_case_insensitive():
    p = v.validate_concept("Net Revenue", "METRIC", "Recognized revenue.", synonyms=["Net Sales", "net sales", " "])
    assert p.synonyms == ["Net Sales"]


def test_apply_edit_rejects_noop_and_unknown_fields():
    cur = {"definition": "Recognized revenue after refunds.", "synonyms": [], "formula": None, "exclusions": [], "formula_provenance": None}
    with pytest.raises(v.ValidationError, match="identical"):
        v.apply_edit("METRIC", cur, {"definition": "Recognized revenue after refunds."})
    with pytest.raises(v.ValidationError, match="non-editable"):
        v.apply_edit("METRIC", cur, {"kind": "TERM"})
    nxt = v.apply_edit("METRIC", cur, {"synonyms": ["net sales"]})
    assert nxt["synonyms"] == ["net sales"]


# -------------------------------------------------------- relationships
C = lambda i, d="D1", k="METRIC": {"concept_id": i, "domain_id": d, "kind": k}  # noqa: E731


def test_custom_requires_label_and_only_custom_has_label():
    with pytest.raises(v.ValidationError, match="label"):
        v.validate_relationship("CUSTOM", C("a"), C("b"))
    with pytest.raises(v.ValidationError, match="only CUSTOM"):
        v.validate_relationship("IS_A", C("a"), C("b"), "BILLED_TO")
    assert v.validate_relationship("custom", C("a", k="ENTITY"), C("b", k="ENTITY"), "billed_to") == ("CUSTOM", "BILLED_TO")


def test_equivalent_to_is_cross_domain_only():
    with pytest.raises(v.ValidationError, match="merged"):
        v.validate_relationship("EQUIVALENT_TO", C("a"), C("b"))
    v.validate_relationship("EQUIVALENT_TO", C("a"), C("b", "D2"))


def test_typed_edge_kind_rules():
    with pytest.raises(v.ValidationError, match="POLICY"):
        v.validate_relationship("APPLIES_TO", C("a", k="TERM"), C("b"))
    with pytest.raises(v.ValidationError, match="DERIVES must target"):
        v.validate_relationship("DERIVES", C("a"), C("b", k="ENTITY"))
    with pytest.raises(v.ValidationError, match="itself"):
        v.validate_relationship("RELATED_TO", C("a"), C("a"))


def test_cycle_detection():
    edges = [("a", "b"), ("b", "c")]
    assert v.would_create_cycle(edges, "c", "a")
    assert not v.would_create_cycle(edges, "a", "c")


# ------------------------------------------------------- representations
def test_representation_rules():
    assert v.validate_representation("implements", "sv_metric", "db.s.sv", "invoices.net_revenue") == (
        "IMPLEMENTS", "SV_METRIC", "DB.S.SV", "INVOICES.NET_REVENUE")
    with pytest.raises(v.ValidationError, match="asset_member"):
        v.validate_representation("IMPLEMENTS", "SV_METRIC", "DB.S.SV")
    with pytest.raises(v.ValidationError, match="fully-qualified"):
        v.validate_representation("IMPLEMENTS", "TABLE", "S.T")
    with pytest.raises(v.ValidationError, match="fully-qualified"):
        v.validate_representation("IMPLEMENTS", "TABLE", "DB.S.T; DROP TABLE X")
    with pytest.raises(v.ValidationError, match="cannot IMPLEMENT"):
        v.validate_representation("IMPLEMENTS", "BI_ASSET", "https://bi/dash/1")
    with pytest.raises(v.ValidationError, match="RELATED_DASHBOARD"):
        v.validate_representation("RELATED_DASHBOARD", "VIEW", "DB.S.V")


def test_classify_drift():
    base = {"expression": "SUM(a)", "comment": "x"}
    assert v.classify_drift(base, None) == ["ASSET_MISSING"]
    assert v.classify_drift(base, dict(base)) == []
    assert v.classify_drift(base, {"expression": "SUM(b)", "comment": "y"}) == ["FORMULA_CHANGED", "DESCRIPTION_CHANGED"]


# ---------------------------------------------------------------- import
ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_parse_native_yaml_and_shorthand():
    docs = importer.parse((ROOT / "seed/glossary/finance.yaml").read_text(), "AUTO", "finance.yaml")
    d = docs[0]
    assert d["domain"] == "Finance"
    assert {c["name"] for c in d["concepts"]} >= {"Net Revenue", "Gross Revenue"}
    assert d["relationships"][0]["source"] == "Finance.Gross Revenue"
    rep = next(r for r in d["representations"] if r["concept"] == "Finance.Net Revenue")
    assert rep["asset_kind"] == "SV_METRIC" and rep["member"] == "INVOICES.NET_REVENUE"
    for c in d["concepts"]:  # every seed concept must pass validation
        v.validate_concept(c["name"], c["kind"], c["definition"], c.get("synonyms"), c.get("formula"),
                           c.get("exclusions"), c.get("formula_provenance"), c.get("scope"))


def test_parse_skos():
    pytest.importorskip("rdflib")
    d = importer.parse((ROOT / "seed/glossary/finance_emea.ttl").read_text(), "AUTO", "x.ttl", "Finance.EMEA")[0]
    names = {c["name"]: c for c in d["concepts"]}
    assert names["EMEA Net Revenue"]["kind"] == "METRIC"
    assert "value added tax" in names["VAT"]["synonyms"]
    assert d["relationships"] == [{"source": "Finance.EMEA.EMEA Net Revenue", "type": "RELATED_TO", "target": "Finance.EMEA.VAT"}]


def test_skos_requires_domain():
    with pytest.raises(v.ValidationError):
        importer.parse("", "SKOS", "x.ttl", None)


# ------------------------------------------------------------------ repo
def test_bind_renders_none_as_null():
    assert _bind("A=? AND B=? AND C=?", [1, None, "x"]) == ("A=? AND B=NULL AND C=?", [1, "x"])
    assert _bind("A=?", [1]) == ("A=?", [1])
    with pytest.raises(ValueError):
        _bind("A=?", [None, None])
