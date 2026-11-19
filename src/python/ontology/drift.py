"""Representation snapshots and drift detection.

A representation stores a snapshot (and its fingerprint) of the asset it
points at when it is proposed. Scheduled validation re-snapshots every live
representation and records findings when the asset disappears or its
expression / description / schema changes. Baselines are only advanced when
a steward acknowledges the finding, so drift never silently "heals".
"""

from __future__ import annotations

import json
from typing import Optional

from . import repo
from .validation import classify_drift, fingerprint


def _describe(session, kind: str, fqn: str) -> list[dict]:
    # fqn is validated against a strict DB.SCHEMA.OBJECT regex before reaching here.
    return [r.as_dict() for r in session.sql(f"DESCRIBE {kind} {fqn}").collect()]


def _semantic_members(session, fqn: str) -> dict[str, dict]:
    """Map 'PARENT.NAME' -> {object_kind, expression, comment, data_type, synonyms}."""
    members: dict[str, dict] = {}
    for r in _describe(session, "SEMANTIC VIEW", fqn):
        kind = (r.get("object_kind") or "").upper()
        if kind not in ("DIMENSION", "FACT", "METRIC", "DERIVED_METRIC"):
            continue
        parent = (r.get("parent_entity") or "").upper()
        key = f"{parent}.{(r.get('object_name') or '').upper()}" if parent else (r.get("object_name") or "").upper()
        m = members.setdefault(key, {"object_kind": kind})
        prop = (r.get("property") or "").upper()
        val = r.get("property_value")
        if prop == "EXPRESSION":
            m["expression"] = val
        elif prop == "COMMENT":
            m["comment"] = val
        elif prop == "DATA_TYPE":
            m["data_type"] = val
        elif prop == "SYNONYMS":
            m["synonyms"] = val
    return members


_SV_KIND = {"SV_DIMENSION": ("DIMENSION",), "SV_FACT": ("FACT",), "SV_METRIC": ("METRIC", "DERIVED_METRIC")}


def snapshot(session, asset_kind: str, fqn: str, member: Optional[str]) -> Optional[dict]:
    """Return a normalized snapshot, or None when the asset does not exist / is not visible."""
    try:
        if asset_kind == "BI_ASSET":
            return {"external": True, "reference": fqn}
        if asset_kind in ("TABLE", "VIEW"):
            cols = _describe(session, asset_kind, fqn)
            return {"columns": sorted([[c["name"], c["type"]] for c in cols])}
        if asset_kind == "COLUMN":
            for c in _describe(session, "TABLE", fqn):
                if c["name"].upper() == member.upper():
                    return {"data_type": c["type"], "comment": c.get("comment")}
            return None
        members = _semantic_members(session, fqn)
        if asset_kind == "SEMANTIC_VIEW":
            return {"columns": sorted(members.keys())}
        m = members.get(member.upper())
        if m is None and "." not in member:
            hits = [v for k, v in members.items() if k.endswith("." + member.upper())]
            m = hits[0] if len(hits) == 1 else None
        if m is None or m["object_kind"] not in _SV_KIND[asset_kind]:
            return None
        return {k: m.get(k) for k in ("expression", "comment", "data_type", "object_kind")}
    except Exception:
        return None


def validate_all(session, domain_path: Optional[str] = None) -> dict:
    params: list = []
    where = "R.STATUS IN ('DRAFT','APPROVED')"
    if domain_path:
        where += " AND UPPER(D.PATH) = UPPER(?)"
        params.append(domain_path)
    reps = repo.rows(
        session,
        f"""SELECT R.*, D.PATH FROM {repo.t(session, 'REPRESENTATION')} R
              JOIN {repo.t(session, 'DOMAIN')} D ON D.DOMAIN_ID = R.DOMAIN_ID WHERE {where}""",
        params,
    )
    stats = {"checked": 0, "valid": 0, "drifted": 0, "missing": 0, "new_findings": 0}
    for r in reps:
        stats["checked"] += 1
        old = json.loads(r["LAST_SNAPSHOT"]) if r.get("LAST_SNAPSHOT") else None
        new = snapshot(session, r["ASSET_KIND"], r["ASSET_FQN"], r.get("ASSET_MEMBER"))
        findings = classify_drift(old, new)
        status = "MISSING" if "ASSET_MISSING" in findings else ("DRIFTED" if findings else "VALID")
        stats[status.lower()] += 1
        for f in findings:
            exists = repo.one(
                session,
                f"""SELECT 1 AS X FROM {repo.t(session, 'DRIFT_FINDING')}
                     WHERE REP_ID = ? AND FINDING_TYPE = ? AND RESOLVED_AT IS NULL""",
                [r["REP_ID"], f],
            )
            if exists:
                continue
            repo.execute(
                session,
                f"""INSERT INTO {repo.t(session, 'DRIFT_FINDING')}
                    (FINDING_ID, REP_ID, CONCEPT_ID, DOMAIN_ID, FINDING_TYPE, BASELINE, OBSERVED, DETECTED_AT)
                    SELECT UUID_STRING(), ?, ?, ?, ?, PARSE_JSON(?), PARSE_JSON(?), CURRENT_TIMESTAMP()""",
                [r["REP_ID"], r["CONCEPT_ID"], r["DOMAIN_ID"], f, repo.jparam(old), repo.jparam(new)],
            )
            stats["new_findings"] += 1
        if old is None and new is not None:
            # First successful validation establishes the baseline.
            repo.execute(
                session,
                f"""UPDATE {repo.t(session, 'REPRESENTATION')}
                       SET LAST_SNAPSHOT = PARSE_JSON(?), ASSET_FINGERPRINT = ? WHERE REP_ID = ?""",
                [repo.jparam(new), fingerprint(new), r["REP_ID"]],
            )
        repo.execute(
            session,
            f"""UPDATE {repo.t(session, 'REPRESENTATION')}
                   SET VALIDATION_STATUS = ?, LAST_VALIDATED_AT = CURRENT_TIMESTAMP() WHERE REP_ID = ?""",
            [status, r["REP_ID"]],
        )
    if stats["new_findings"]:
        repo.audit(session, "DRIFT_DETECTED", "REPRESENTATION", None, None, stats)
    return stats
