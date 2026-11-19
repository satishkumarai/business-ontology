"""Export approved, caller-visible concepts.

Runs with CALLER's rights and reads only API views, so row access policies
restrict the export to what the caller is entitled to see.
"""

from __future__ import annotations

import json
from typing import Optional

from . import repo
from .validation import ValidationError

ONT_NS = "urn:snowflake:business-ontology#"


def _arr(v):
    if v is None:
        return []
    return json.loads(v) if isinstance(v, str) else list(v)


def collect(session, domain_path: Optional[str]) -> dict:
    db = repo.db(session)
    flt, params = "", []
    if domain_path:
        flt = " WHERE UPPER(DOMAIN_PATH) = UPPER(?) OR STARTSWITH(UPPER(DOMAIN_PATH), UPPER(?) || '.')"
        params = [domain_path, domain_path]
    concepts = [r.as_dict() for r in session.sql(f"SELECT * FROM {db}.API.V_CONCEPT{flt} ORDER BY QUALIFIED_NAME", params=params).collect()]
    rels = [r.as_dict() for r in session.sql(f"SELECT * FROM {db}.API.V_RELATIONSHIP ORDER BY SOURCE_QNAME").collect()]
    reps = [r.as_dict() for r in session.sql(f"SELECT * FROM {db}.API.V_REPRESENTATION ORDER BY QUALIFIED_NAME").collect()]
    names = {c["QUALIFIED_NAME"] for c in concepts}
    by_domain: dict[str, dict] = {}
    for c in concepts:
        d = by_domain.setdefault(c["DOMAIN_PATH"], {"domain": c["DOMAIN_PATH"], "concepts": [], "relationships": [], "representations": []})
        item = {"name": c["NAME"], "kind": c["KIND"], "definition": c["DEFINITION"]}
        for k_src, k_dst in (("SCOPE", "scope"), ("FORMULA", "formula"), ("FORMULA_PROVENANCE", "formula_provenance")):
            if c.get(k_src):
                item[k_dst] = c[k_src]
        for k_src, k_dst in (("SYNONYMS", "synonyms"), ("EXCLUSIONS", "exclusions")):
            if _arr(c.get(k_src)):
                item[k_dst] = _arr(c[k_src])
        item["version"] = c["VERSION_NO"]
        item["approved_by"] = c["APPROVED_BY"]
        d["concepts"].append(item)
    for r in rels:
        if r["SOURCE_QNAME"] in names and r["TARGET_QNAME"] in names:
            e = {"source": r["SOURCE_QNAME"], "type": r["REL_TYPE"], "target": r["TARGET_QNAME"]}
            if r.get("CUSTOM_LABEL"):
                e["label"] = r["CUSTOM_LABEL"]
            by_domain[r["SOURCE_DOMAIN"]]["relationships"].append(e)
    for r in reps:
        if r["QUALIFIED_NAME"] in names:
            e = {"concept": r["QUALIFIED_NAME"], "type": r["REP_TYPE"], "asset_kind": r["ASSET_KIND"], "asset": r["ASSET_FQN"]}
            if r.get("ASSET_MEMBER"):
                e["member"] = r["ASSET_MEMBER"]
            by_domain[r["DOMAIN_PATH"]]["representations"].append(e)
    return {"documents": list(by_domain.values())}


def _ttl_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def to_turtle(doc: dict) -> str:
    def iri(qname: str) -> str:
        from urllib.parse import quote

        return f"<{ONT_NS}{quote(qname, safe='')}>"

    out = [
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
        f"@prefix ont: <{ONT_NS}> .",
        "",
    ]
    skos_map = {"IS_A": "skos:broader", "RELATED_TO": "skos:related", "EQUIVALENT_TO": "skos:exactMatch"}
    for d in doc["documents"]:
        scheme = iri(d["domain"])
        out.append(f"{scheme} a skos:ConceptScheme ; skos:prefLabel {_ttl_str(d['domain'])} .")
        for c in d["concepts"]:
            qn = f"{d['domain']}.{c['name']}" + (f" [{c['scope']}]" if c.get("scope") else "")
            lines = [f"{iri(qn)} a skos:Concept", f"skos:inScheme {scheme}", f"skos:prefLabel {_ttl_str(c['name'])}",
                     f"skos:definition {_ttl_str(c['definition'])}", f"ont:kind {_ttl_str(c['kind'])}"]
            lines += [f"skos:altLabel {_ttl_str(s)}" for s in c.get("synonyms", [])]
            if c.get("formula"):
                lines.append(f"ont:formula {_ttl_str(c['formula'])}")
            out.append(" ;\n    ".join(lines) + " .")
        for r in d["relationships"]:
            pred = skos_map.get(r["type"], f"ont:{(r.get('label') or r['type']).lower()}")
            out.append(f"{iri(r['source'])} {pred} {iri(r['target'])} .")
        out.append("")
    return "\n".join(out)


def export_ontology(session, domain_path=None, fmt="JSON"):
    fmt = (fmt or "JSON").upper()
    doc = collect(session, domain_path)
    if fmt == "JSON":
        return doc
    if fmt == "YAML":
        import yaml

        return {"format": "YAML", "content": yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)}
    if fmt in ("TURTLE", "SKOS", "RDF"):
        return {"format": "TURTLE", "content": to_turtle(doc)}
    raise ValidationError("fmt must be JSON, YAML or TURTLE")
