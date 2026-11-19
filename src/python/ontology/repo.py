"""Data-access helpers. All SQL is parameterized; identifiers are validated first."""

from __future__ import annotations

import json
from typing import Any, Optional

from .validation import NotFound, parse_qualified_name

DB = None  # resolved lazily from CURRENT_DATABASE() of the owning procedure


def db(session) -> str:
    global DB
    if DB is None:
        try:
            from ._deployment import DB as deployed_db  # written by scripts/deploy.py
            DB = deployed_db
        except ImportError:  # local / unit tests
            DB = session.sql("SELECT CURRENT_DATABASE()").collect()[0][0]
    return DB


def t(session, name: str) -> str:
    return f"{db(session)}.CORE.{name}"


def _bind(sql: str, params: Optional[list]) -> tuple[str, list]:
    """Snowpark binds Python None as the string 'None'; render those as SQL NULL.

    Our SQL never contains '?' inside string literals, so positional
    substitution is safe.
    """
    if not params or all(p is not None for p in params):
        return sql, list(params or [])
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError("parameter count mismatch")
    out, kept = [parts[0]], []
    for p, tail in zip(params, parts[1:]):
        if p is None:
            out.append("NULL")
        else:
            out.append("?")
            kept.append(p)
        out.append(tail)
    return "".join(out), kept


def rows(session, sql: str, params: Optional[list] = None) -> list[dict]:
    sql, params = _bind(sql, params)
    res = session.sql(sql, params=params).collect()
    return [r.as_dict() for r in res]


def one(session, sql: str, params: Optional[list] = None) -> Optional[dict]:
    r = rows(session, sql, params)
    return r[0] if r else None


def execute(session, sql: str, params: Optional[list] = None) -> None:
    sql, params = _bind(sql, params)
    session.sql(sql, params=params).collect()


def current_user(session) -> str:
    return session.sql("SELECT CURRENT_USER()").collect()[0][0]


def setting(session, key: str, default: Any = None) -> Any:
    r = one(session, f"SELECT VALUE FROM {t(session, 'SETTINGS')} WHERE KEY = ?", [key])
    if not r or r["VALUE"] is None:
        return default
    return json.loads(r["VALUE"])


def jparam(value) -> str:
    """Serialize a Python value for PARSE_JSON(?) binding."""
    return json.dumps(value, default=str)


def audit(session, action: str, entity_type: str, entity_id: str, domain_id: Optional[str], details: dict) -> None:
    execute(
        session,
        f"""INSERT INTO {t(session, 'AUDIT_EVENT')}
            (EVENT_ID, EVENT_TS, ACTOR_USER, ACTION, ENTITY_TYPE, ENTITY_ID, DOMAIN_ID, DETAILS)
            SELECT UUID_STRING(), CURRENT_TIMESTAMP(), CURRENT_USER(), ?, ?, ?, ?, PARSE_JSON(?)""",
        [action, entity_type, entity_id, domain_id, jparam(details)],
    )


# ----------------------------------------------------------------- domains


def get_domain(session, path: str) -> dict:
    d = one(
        session,
        f"SELECT * FROM {t(session, 'DOMAIN')} WHERE UPPER(PATH) = UPPER(?) AND IS_ACTIVE",
        [path],
    )
    if not d:
        raise NotFound(f"domain '{path}' does not exist")
    return d


def get_domain_by_id(session, domain_id: str) -> dict:
    d = one(session, f"SELECT * FROM {t(session, 'DOMAIN')} WHERE DOMAIN_ID = ?", [domain_id])
    if not d:
        raise NotFound(f"domain id '{domain_id}' does not exist")
    return d


# ---------------------------------------------------------------- concepts


def find_concept(session, qname: str, include_inactive: bool = False) -> Optional[dict]:
    domain_path, name, scope = parse_qualified_name(qname)
    status_clause = "" if include_inactive else "AND C.STATUS IN ('DRAFT','APPROVED')"
    return one(
        session,
        f"""SELECT C.*, D.PATH AS DOMAIN_PATH
              FROM {t(session, 'CONCEPT')} C
              JOIN {t(session, 'DOMAIN')} D ON D.DOMAIN_ID = C.DOMAIN_ID
             WHERE UPPER(D.PATH) = UPPER(?)
               AND UPPER(C.NAME) = UPPER(?)
               AND EQUAL_NULL(UPPER(C.SCOPE), UPPER(?))
               {status_clause}""",
        [domain_path, name, scope],
    )


def get_concept(session, qname: str) -> dict:
    c = find_concept(session, qname)
    if not c:
        raise NotFound(f"concept '{qname}' does not exist or is not visible to you")
    return c


def get_concept_by_id(session, concept_id: str) -> dict:
    c = one(
        session,
        f"""SELECT C.*, D.PATH AS DOMAIN_PATH FROM {t(session, 'CONCEPT')} C
            JOIN {t(session, 'DOMAIN')} D ON D.DOMAIN_ID = C.DOMAIN_ID WHERE C.CONCEPT_ID = ?""",
        [concept_id],
    )
    if not c:
        raise NotFound(f"concept id '{concept_id}' does not exist")
    return c


def get_version(session, version_id: str) -> dict:
    v = one(session, f"SELECT * FROM {t(session, 'CONCEPT_VERSION')} WHERE VERSION_ID = ?", [version_id])
    if not v:
        raise NotFound(f"version '{version_id}' does not exist")
    return v


def latest_version(session, concept_id: str) -> dict:
    return one(
        session,
        f"""SELECT * FROM {t(session, 'CONCEPT_VERSION')} WHERE CONCEPT_ID = ?
            ORDER BY VERSION_NO DESC LIMIT 1""",
        [concept_id],
    )


def version_fields(v: dict) -> dict:
    def arr(x):
        if x is None:
            return []
        return json.loads(x) if isinstance(x, str) else list(x)

    return {
        "definition": v.get("DEFINITION"),
        "synonyms": arr(v.get("SYNONYMS")),
        "formula": v.get("FORMULA"),
        "exclusions": arr(v.get("EXCLUSIONS")),
        "formula_provenance": v.get("FORMULA_PROVENANCE"),
    }
