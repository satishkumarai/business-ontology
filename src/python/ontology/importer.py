"""Glossary / ontology import.

Supported formats
-----------------
YAML / JSON  Native document (see seed/glossary/*.yaml). Either a single
             ``domain:`` document or ``documents: [...]`` of them. Accepts the
             shorthand ``semantic_view: + metric:`` representation form.
SKOS (TTL)   skos:Concept with prefLabel / definition / altLabel / broader /
             related / exactMatch. ``ont:kind`` overrides the default TERM.

Everything imported lands as DRAFT with SOURCE='IMPORT'; stewards approve.
"""

from __future__ import annotations

import json
from typing import Optional

from .validation import ValidationError, normalize_domain_path

ONT_NS = "urn:snowflake:business-ontology#"


def read_stage_file(session, stage_path: str) -> str:
    if not stage_path or not stage_path.startswith("@"):
        raise ValidationError("stage_path must look like @DB.SCHEMA.STAGE/path/file.yaml")
    with session.file.get_stream(stage_path) as f:
        return f.read().decode("utf-8")


def _detect(fmt: str, path: str) -> str:
    fmt = (fmt or "AUTO").upper()
    if fmt != "AUTO":
        return fmt
    p = path.lower()
    if p.endswith((".yaml", ".yml")):
        return "YAML"
    if p.endswith(".json"):
        return "JSON"
    if p.endswith((".ttl", ".rdf", ".owl", ".xml", ".nt")):
        return "SKOS"
    raise ValidationError("cannot infer format from file extension; pass fmt")


def _qualify(ref: str, domain: str) -> str:
    """Bare concept names in a document resolve to that document's domain."""
    return ref if "." in ref else f"{domain}.{ref}"


def normalize_document(doc: dict, default_domain: Optional[str] = None) -> dict:
    domain = normalize_domain_path(doc.get("domain") or default_domain or "")
    concepts = doc.get("concepts") or []
    rels, reps = [], []
    for r in doc.get("relationships") or []:
        rels.append({
            "source": _qualify(r["source"], domain),
            "type": r["type"],
            "target": _qualify(r["target"], domain),
            "label": r.get("label"),
        })
    for r in doc.get("representations") or []:
        if "semantic_view" in r:  # shorthand used in the product blog
            member = r.get("metric") or r.get("dimension") or r.get("fact")
            kind = "SV_METRIC" if r.get("metric") else "SV_DIMENSION" if r.get("dimension") else "SV_FACT" if r.get("fact") else "SEMANTIC_VIEW"
            reps.append({"concept": _qualify(r["concept"], domain), "type": r.get("type", "IMPLEMENTS"),
                         "asset_kind": kind, "asset": r["semantic_view"], "member": member})
        else:
            reps.append({"concept": _qualify(r["concept"], domain), "type": r.get("type", "IMPLEMENTS"),
                         "asset_kind": r["asset_kind"], "asset": r["asset"], "member": r.get("member")})
    return {"domain": domain, "concepts": concepts, "relationships": rels, "representations": reps}


def parse(content: str, fmt: str, path: str, default_domain: Optional[str] = None) -> list[dict]:
    fmt = _detect(fmt, path)
    if fmt in ("YAML", "JSON"):
        if fmt == "YAML":
            import yaml

            raw = yaml.safe_load(content)
        else:
            raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ValidationError("document root must be an object")
        docs = raw.get("documents") or [raw]
        return [normalize_document(d, default_domain) for d in docs]
    if fmt == "SKOS":
        return [parse_skos(content, default_domain)]
    raise ValidationError(f"unsupported format {fmt}")


def parse_skos(content: str, default_domain: Optional[str]) -> dict:
    if not default_domain:
        raise ValidationError("SKOS import requires default_domain")
    import rdflib
    from rdflib.namespace import RDF, SKOS

    g = rdflib.Graph()
    g.parse(data=content, format="turtle")
    ont = rdflib.Namespace(ONT_NS)
    domain = normalize_domain_path(default_domain)
    label: dict = {}
    concepts = []
    for c in g.subjects(RDF.type, SKOS.Concept):
        name = str(g.value(c, SKOS.prefLabel) or "").strip()
        if not name:
            continue
        label[c] = name
        concepts.append({
            "name": name,
            "kind": str(g.value(c, ont.kind) or "TERM").upper(),
            "definition": str(g.value(c, SKOS.definition) or ""),
            "synonyms": sorted(str(a) for a in g.objects(c, SKOS.altLabel)),
            "formula": (str(g.value(c, ont.formula)) if g.value(c, ont.formula) else None),
        })
    rels = []
    for pred, rtype in ((SKOS.broader, "IS_A"), (SKOS.related, "RELATED_TO"), (SKOS.exactMatch, "EQUIVALENT_TO")):
        for s, o in g.subject_objects(pred):
            if s in label and o in label:
                rels.append({"source": f"{domain}.{label[s]}", "type": rtype, "target": f"{domain}.{label[o]}"})
    return {"domain": domain, "concepts": concepts, "relationships": rels, "representations": []}
