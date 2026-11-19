"""Pure validation rules for the governed ontology.

Nothing in this module touches Snowflake, so every rule can be unit-tested
locally (see tests/unit). The stored-procedure layer calls these before any
write so that invalid state never reaches the CORE tables.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

KINDS = ("TERM", "METRIC", "ENTITY", "ACTION", "POLICY", "DIMENSION", "MEASURE")
REL_TYPES = (
    "IS_A",
    "HAS_PART",
    "DERIVES",
    "MEASURES",
    "APPLIES_TO",
    "EQUIVALENT_TO",
    "RELATED_TO",
    "CUSTOM",
)
REP_TYPES = ("IMPLEMENTS", "DESCRIBES", "RELATED_DASHBOARD")
ASSET_KINDS = (
    "TABLE",
    "VIEW",
    "COLUMN",
    "SEMANTIC_VIEW",
    "SV_DIMENSION",
    "SV_FACT",
    "SV_METRIC",
    "BI_ASSET",
)
SOURCES = ("MANUAL", "IMPORT", "SEMANTIC_VIEW_BOOTSTRAP", "CORTEX_SENSE")
PRIVILEGES = ("USAGE", "READ", "SUGGEST", "MODIFY")

# Concepts that may carry a formula / exclusions / formula provenance.
FORMULA_KINDS = ("METRIC",)
# Kinds that are quantitative (valid endpoints for DERIVES / MEASURES).
QUANT_KINDS = ("METRIC", "MEASURE")
# Fields a PROPOSE_EDIT may change.
EDITABLE_FIELDS = (
    "definition",
    "synonyms",
    "formula",
    "exclusions",
    "formula_provenance",
)

_SEGMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _&-]{0,62}$")
_NAME_RE = re.compile(r"^[^.\[\]]{1,200}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9 _&-]{1,64}$")
_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
# Fully-qualified object names: DB.SCHEMA.OBJ, optionally double-quoted parts.
_FQN_PART = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")'
_FQN_RE = re.compile(rf"^{_FQN_PART}\.{_FQN_PART}\.{_FQN_PART}$")
_MEMBER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


class OntologyError(Exception):
    """Base error. ``code`` is stable and machine-readable."""

    code = "ONTOLOGY_ERROR"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")
        self.message = message


class ValidationError(OntologyError):
    code = "VALIDATION_ERROR"


class NotAuthorized(OntologyError):
    code = "NOT_AUTHORIZED"


class NotFound(OntologyError):
    code = "NOT_FOUND"


class Conflict(OntologyError):
    code = "CONFLICT"


# --------------------------------------------------------------------------
# Domains and names
# --------------------------------------------------------------------------


def parse_domain_path(path: str) -> list[str]:
    if not path or not isinstance(path, str):
        raise ValidationError("domain path is required")
    segments = [s.strip() for s in path.split(".")]
    if len(segments) > 8:
        raise ValidationError("domain nesting is limited to 8 levels")
    for seg in segments:
        if not _SEGMENT_RE.match(seg):
            raise ValidationError(
                f"invalid domain segment '{seg}': start with a letter; "
                "letters, digits, space, _ & - only; max 63 chars"
            )
    return segments


def normalize_domain_path(path: str) -> str:
    return ".".join(parse_domain_path(path))


def role_name_for(domain_path: str, privilege: str, prefix: str = "ONT") -> str:
    """Deterministic Snowflake role name for a domain privilege."""
    privilege = privilege.upper()
    if privilege not in PRIVILEGES:
        raise ValidationError(f"privilege must be one of {PRIVILEGES}")
    body = re.sub(r"[^A-Z0-9]+", "_", normalize_domain_path(domain_path).upper()).strip("_")
    name = f"{prefix}_{body}_{privilege}"
    if len(name) > 255:
        raise ValidationError("generated role name too long")
    return name


def validate_role_name(role: str) -> str:
    if not role or not _ROLE_RE.match(role):
        raise ValidationError(f"invalid role name '{role}'")
    return role.upper()


def validate_concept_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValidationError("concept name must be 1-200 chars and may not contain '.', '[' or ']'")
    return name


def validate_scope(scope: Optional[str]) -> Optional[str]:
    if scope is None or str(scope).strip() == "":
        return None
    scope = str(scope).strip()
    if not _SCOPE_RE.match(scope):
        raise ValidationError(f"invalid scope '{scope}'")
    return scope


def qualified_name(domain_path: str, name: str, scope: Optional[str] = None) -> str:
    qn = f"{normalize_domain_path(domain_path)}.{validate_concept_name(name)}"
    return f"{qn} [{scope}]" if scope else qn


def parse_qualified_name(qname: str) -> tuple[str, str, Optional[str]]:
    """'Finance.Revenue.Net Revenue [EMEA]' -> ('Finance.Revenue', 'Net Revenue', 'EMEA')."""
    if not qname or "." not in qname:
        raise ValidationError(f"'{qname}' is not a domain-qualified concept name (Domain.Concept)")
    qname = qname.strip()
    scope = None
    m = re.match(r"^(.*)\s\[([^\]]+)\]$", qname)
    if m:
        qname, scope = m.group(1), m.group(2)
    domain, _, name = qname.rpartition(".")
    return normalize_domain_path(domain), validate_concept_name(name), validate_scope(scope)


# --------------------------------------------------------------------------
# Concepts
# --------------------------------------------------------------------------


def _as_str_list(value, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else [value]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field_name} must be a list of strings")
    out: list[str] = []
    for v in value:
        s = str(v).strip()
        if s and s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out


@dataclass
class ConceptPayload:
    name: str
    kind: str
    definition: str
    synonyms: list[str] = field(default_factory=list)
    formula: Optional[str] = None
    exclusions: list[str] = field(default_factory=list)
    formula_provenance: Optional[str] = None
    scope: Optional[str] = None

    def as_version_dict(self) -> dict:
        return {
            "definition": self.definition,
            "synonyms": self.synonyms,
            "formula": self.formula,
            "exclusions": self.exclusions,
            "formula_provenance": self.formula_provenance,
        }


def validate_concept(
    name,
    kind,
    definition,
    synonyms=None,
    formula=None,
    exclusions=None,
    formula_provenance=None,
    scope=None,
) -> ConceptPayload:
    kind = (kind or "").strip().upper()
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {KINDS}")
    definition = (definition or "").strip()
    if len(definition) < 10:
        raise ValidationError("definition must be at least 10 characters; say what the concept means")
    if len(definition) > 4000:
        raise ValidationError("definition exceeds 4000 characters")
    formula = (formula or "").strip() or None
    formula_provenance = (formula_provenance or "").strip() or None
    exclusions_l = _as_str_list(exclusions, "exclusions")
    if kind not in FORMULA_KINDS and (formula or exclusions_l or formula_provenance):
        raise ValidationError(
            f"only {FORMULA_KINDS} concepts may carry formula, exclusions or formula_provenance (got {kind})"
        )
    return ConceptPayload(
        name=validate_concept_name(name),
        kind=kind,
        definition=definition,
        synonyms=_as_str_list(synonyms, "synonyms"),
        formula=formula,
        exclusions=exclusions_l,
        formula_provenance=formula_provenance,
        scope=validate_scope(scope),
    )


def apply_edit(kind: str, current: dict, changes: dict) -> dict:
    """Return the next version's fields. Rejects unknown or empty edits."""
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("changes must be a non-empty object")
    unknown = set(k.lower() for k in changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(f"non-editable fields {sorted(unknown)}; editable: {EDITABLE_FIELDS}")
    merged = dict(current)
    for k, v in changes.items():
        merged[k.lower()] = v
    payload = validate_concept(
        name="x",
        kind=kind,
        definition=merged.get("definition"),
        synonyms=merged.get("synonyms"),
        formula=merged.get("formula"),
        exclusions=merged.get("exclusions"),
        formula_provenance=merged.get("formula_provenance"),
    )
    nxt = payload.as_version_dict()
    if all(_norm(nxt[k]) == _norm(current.get(k)) for k in EDITABLE_FIELDS):
        raise ValidationError("proposed edit is identical to the current version")
    return nxt


def _norm(v):
    if v in (None, "", []):
        return None
    return v


def validate_source(source: Optional[str]) -> str:
    source = (source or "MANUAL").upper()
    if source not in SOURCES:
        raise ValidationError(f"source must be one of {SOURCES}")
    return source


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def validate_relationship(
    rel_type: str,
    source: dict,
    target: dict,
    custom_label: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """``source`` / ``target`` are dicts with concept_id, domain_id, kind."""
    rel_type = (rel_type or "").strip().upper()
    if rel_type not in REL_TYPES:
        raise ValidationError(f"relationship type must be one of {REL_TYPES}")
    label = (custom_label or "").strip() or None
    if rel_type == "CUSTOM" and not label:
        raise ValidationError("CUSTOM relationships require a label")
    if rel_type != "CUSTOM" and label:
        raise ValidationError("only CUSTOM relationships may carry a label")
    if label and not re.match(r"^[A-Z][A-Z0-9_]{1,62}$", label.upper()):
        raise ValidationError("custom label must be UPPER_SNAKE_CASE, 2-63 chars")
    if source["concept_id"] == target["concept_id"]:
        raise ValidationError("a concept cannot relate to itself")
    if rel_type == "EQUIVALENT_TO" and source["domain_id"] == target["domain_id"]:
        raise ValidationError(
            "EQUIVALENT_TO links concepts across domains; same-domain duplicates must be merged instead"
        )
    if rel_type == "APPLIES_TO" and source["kind"] != "POLICY":
        raise ValidationError("APPLIES_TO must originate from a POLICY concept")
    if rel_type in ("DERIVES", "MEASURES") and source["kind"] not in QUANT_KINDS + ("ENTITY",):
        raise ValidationError(f"{rel_type} must originate from a METRIC, MEASURE or ENTITY concept")
    if rel_type == "DERIVES" and target["kind"] not in QUANT_KINDS:
        raise ValidationError("DERIVES must target a METRIC or MEASURE concept")
    return rel_type, label.upper() if label else None


def would_create_cycle(edges: Iterable[tuple[str, str]], source_id: str, target_id: str) -> bool:
    """True if adding source->target closes a cycle in a directed acyclic relation (IS_A, HAS_PART, DERIVES)."""
    adj: dict[str, set[str]] = {}
    for s, t in edges:
        adj.setdefault(s, set()).add(t)
    stack, seen = [target_id], set()
    while stack:
        node = stack.pop()
        if node == source_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, ()))
    return False


ACYCLIC_TYPES = ("IS_A", "HAS_PART", "DERIVES")


# --------------------------------------------------------------------------
# Representations
# --------------------------------------------------------------------------


def validate_representation(rep_type, asset_kind, asset_fqn, asset_member=None) -> tuple[str, str, str, Optional[str]]:
    rep_type = (rep_type or "").strip().upper()
    asset_kind = (asset_kind or "").strip().upper()
    if rep_type not in REP_TYPES:
        raise ValidationError(f"representation type must be one of {REP_TYPES}")
    if asset_kind not in ASSET_KINDS:
        raise ValidationError(f"asset kind must be one of {ASSET_KINDS}")
    asset_fqn = (asset_fqn or "").strip()
    asset_member = (asset_member or "").strip() or None
    if asset_kind == "BI_ASSET":
        if not asset_fqn or len(asset_fqn) > 1000:
            raise ValidationError("BI_ASSET requires an asset reference (URL or catalog id)")
        if rep_type == "IMPLEMENTS":
            raise ValidationError("a BI asset cannot IMPLEMENT a concept; use RELATED_DASHBOARD or DESCRIBES")
        return rep_type, asset_kind, asset_fqn, asset_member
    if not _FQN_RE.match(asset_fqn):
        raise ValidationError(f"asset must be a fully-qualified DB.SCHEMA.OBJECT name, got '{asset_fqn}'")
    if asset_kind in ("SV_DIMENSION", "SV_FACT", "SV_METRIC", "COLUMN"):
        if not asset_member or not _MEMBER_RE.match(asset_member):
            raise ValidationError(
                f"{asset_kind} requires asset_member (e.g. INVOICES.NET_REVENUE or COLUMN_NAME)"
            )
    elif asset_member:
        raise ValidationError(f"{asset_kind} does not take asset_member")
    if rep_type == "RELATED_DASHBOARD":
        raise ValidationError("RELATED_DASHBOARD is only valid for BI_ASSET representations")
    return rep_type, asset_kind, asset_fqn.upper() if '"' not in asset_fqn else asset_fqn, (
        asset_member.upper() if asset_member else None
    )


def fingerprint(snapshot) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def classify_drift(old: Optional[dict], new: Optional[dict]) -> list[str]:
    """Compare two representation snapshots; return finding types."""
    if new is None:
        return ["ASSET_MISSING"]
    if old is None:
        return []
    findings = []
    if _norm(old.get("expression")) != _norm(new.get("expression")):
        findings.append("FORMULA_CHANGED")
    if _norm(old.get("comment")) != _norm(new.get("comment")):
        findings.append("DESCRIPTION_CHANGED")
    if _norm(old.get("columns")) != _norm(new.get("columns")):
        findings.append("SCHEMA_CHANGED")
    if _norm(old.get("data_type")) != _norm(new.get("data_type")):
        findings.append("TYPE_CHANGED")
    return findings
