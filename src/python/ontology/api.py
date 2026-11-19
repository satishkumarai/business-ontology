"""Stored-procedure handlers for the governed ontology write path.

Every handler:
  * runs with OWNER's rights (callers have no DML on CORE tables),
  * authorizes the real caller (CURRENT_USER) against domain privileges,
  * validates input with ``validation`` before writing,
  * writes an AUDIT_EVENT row in the same transaction as the change,
  * raises ``OntologyError`` subclasses with stable codes on failure.
"""

from __future__ import annotations

import functools
import json
import re
from typing import Optional

from . import authz, drift, repo
from .validation import (
    ACYCLIC_TYPES,
    PRIVILEGES,
    Conflict,
    NotAuthorized,
    NotFound,
    ValidationError,
    apply_edit,
    fingerprint,
    normalize_domain_path,
    parse_domain_path,
    qualified_name,
    role_name_for,
    validate_concept,
    validate_relationship,
    validate_representation,
    validate_role_name,
    validate_source,
    would_create_cycle,
)


def handler(fn):
    """Reset per-call caches and wrap the body in a transaction."""

    @functools.wraps(fn)
    def wrapper(session, *args):
        authz.reset_cache()
        session.sql("BEGIN").collect()
        try:
            result = fn(session, *args)
            session.sql("COMMIT").collect()
            return result
        except Exception:
            session.sql("ROLLBACK").collect()
            raise

    return wrapper


def _uuid(session) -> str:
    return session.sql("SELECT UUID_STRING()").collect()[0][0]


def _require_distinct_approver(session, author: str, approver: str) -> None:
    if repo.setting(session, "REQUIRE_DISTINCT_APPROVER", True) and author.upper() == approver.upper():
        raise NotAuthorized("separation of duties: the author of a change cannot approve it")


# =========================================================================
# Domains
# =========================================================================


@handler
def create_domain(session, path, description=None, owner_role=None, steward_role=None):
    user = authz.require_admin(session)
    segments = parse_domain_path(path)
    path = ".".join(segments)
    if repo.one(session, f"SELECT 1 AS X FROM {repo.t(session, 'DOMAIN')} WHERE UPPER(PATH)=UPPER(?)", [path]):
        raise Conflict(f"domain '{path}' already exists")
    parent = repo.get_domain(session, ".".join(segments[:-1])) if len(segments) > 1 else None
    owner_role = validate_role_name(owner_role) if owner_role else None
    steward_role = validate_role_name(steward_role) if steward_role else None

    domain_id = _uuid(session)
    prefix = repo.setting(session, "ROLE_PREFIX", "ONT")
    consumer = repo.setting(session, "CONSUMER_ROLE", "ONTOLOGY_CONSUMER")
    db = repo.db(session)
    roles = {p: role_name_for(path, p, prefix) for p in PRIVILEGES}
    for p, r in roles.items():
        session.sql(
            f"CREATE ROLE IF NOT EXISTS {r} COMMENT = 'Business Ontology {p} on domain {path} (managed)'"
        ).collect()
    session.sql(f"GRANT ROLE {consumer} TO ROLE {roles['USAGE']}").collect()
    session.sql(f"GRANT ROLE {roles['USAGE']} TO ROLE {roles['READ']}").collect()
    session.sql(f"GRANT ROLE {roles['READ']} TO ROLE {roles['SUGGEST']}").collect()
    session.sql(f"GRANT ROLE {roles['SUGGEST']} TO ROLE {roles['MODIFY']}").collect()
    session.sql(f"GRANT READ ON STAGE {db}.API.IMPORT_STAGE TO ROLE {roles['SUGGEST']}").collect()
    session.sql(f"GRANT WRITE ON STAGE {db}.API.IMPORT_STAGE TO ROLE {roles['SUGGEST']}").collect()
    if steward_role:
        session.sql(f"GRANT ROLE {roles['MODIFY']} TO ROLE {steward_role}").collect()
    if owner_role:
        session.sql(f"GRANT ROLE {roles['READ']} TO ROLE {owner_role}").collect()

    # Role DDL above auto-commits (and is idempotent); make the metadata writes atomic.
    session.sql("BEGIN").collect()
    repo.execute(
        session,
        f"""INSERT INTO {repo.t(session, 'DOMAIN')}
            (DOMAIN_ID, NAME, PATH, PARENT_DOMAIN_ID, DESCRIPTION, OWNER_ROLE, STEWARD_ROLE, CREATED_BY, CREATED_AT, IS_ACTIVE)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP(), TRUE""",
        [domain_id, segments[-1], path, parent["DOMAIN_ID"] if parent else None, description, owner_role, steward_role, user],
    )
    for p, r in roles.items():
        repo.execute(
            session,
            f"INSERT INTO {repo.t(session, 'DOMAIN_ACCESS')} (DOMAIN_ID, PRIVILEGE, ROLE_NAME, IS_MANAGED) SELECT ?, ?, ?, TRUE",
            [domain_id, p, r],
        )
    repo.execute(
        session,
        f"""INSERT INTO {repo.t(session, 'DOMAIN_ANCESTOR')} (DOMAIN_ID, ANCESTOR_ID, DEPTH)
            SELECT ?, ?, 0
            UNION ALL
            SELECT ?, ANCESTOR_ID, DEPTH + 1 FROM {repo.t(session, 'DOMAIN_ANCESTOR')} WHERE DOMAIN_ID = ?""",
        [domain_id, domain_id, domain_id, parent["DOMAIN_ID"] if parent else "<none>"],
    )
    repo.audit(session, "DOMAIN_CREATED", "DOMAIN", domain_id, domain_id,
               {"path": path, "owner_role": owner_role, "steward_role": steward_role, "roles": roles})
    return {"domain_id": domain_id, "path": path, "roles": roles}


def _require_owner_or_admin(session, domain: dict) -> str:
    user = repo.current_user(session)
    if authz.is_admin(session, user):
        return user
    if domain.get("OWNER_ROLE") and authz.user_holds_role(session, domain["OWNER_ROLE"], user):
        return user
    raise NotAuthorized(f"only the owner of domain '{domain['PATH']}' or an ontology admin can do this")


@handler
def set_domain_privilege(session, path, privilege, grantee_role, action="GRANT"):
    domain = repo.get_domain(session, path)
    user = _require_owner_or_admin(session, domain)
    privilege = (privilege or "").upper()
    if privilege not in PRIVILEGES:
        raise ValidationError(f"privilege must be one of {PRIVILEGES}")
    action = (action or "GRANT").upper()
    if action not in ("GRANT", "REVOKE"):
        raise ValidationError("action must be GRANT or REVOKE")
    grantee_role = validate_role_name(grantee_role)
    role = authz.domain_privilege_roles(session, domain["DOMAIN_ID"], privilege)[0]
    verb = "GRANT ROLE {r} TO ROLE {g}" if action == "GRANT" else "REVOKE ROLE {r} FROM ROLE {g}"
    session.sql(verb.format(r=role, g=grantee_role)).collect()
    repo.audit(session, f"PRIVILEGE_{action}", "DOMAIN", domain["DOMAIN_ID"], domain["DOMAIN_ID"],
               {"privilege": privilege, "role": role, "grantee_role": grantee_role, "by": user})
    return {"domain": domain["PATH"], "privilege": privilege, "role": role, "grantee_role": grantee_role, "action": action}


@handler
def drop_domain(session, path):
    domain = repo.get_domain(session, path)
    _require_owner_or_admin(session, domain)
    live = repo.one(
        session,
        f"""SELECT COUNT(*) AS N FROM {repo.t(session, 'CONCEPT')}
             WHERE DOMAIN_ID = ? AND STATUS IN ('DRAFT','APPROVED')""",
        [domain["DOMAIN_ID"]],
    )["N"]
    children = repo.one(
        session,
        f"SELECT COUNT(*) AS N FROM {repo.t(session, 'DOMAIN')} WHERE PARENT_DOMAIN_ID = ? AND IS_ACTIVE",
        [domain["DOMAIN_ID"]],
    )["N"]
    if live or children:
        raise Conflict(f"domain '{path}' still has {live} live concept(s) and {children} child domain(s)")
    for r in authz.domain_privilege_roles(session, domain["DOMAIN_ID"], "MODIFY") + \
            authz.domain_privilege_roles(session, domain["DOMAIN_ID"], "SUGGEST") + \
            authz.domain_privilege_roles(session, domain["DOMAIN_ID"], "READ") + \
            authz.domain_privilege_roles(session, domain["DOMAIN_ID"], "USAGE"):
        session.sql(f"DROP ROLE IF EXISTS {validate_role_name(r)}").collect()
    repo.execute(session, f"UPDATE {repo.t(session, 'DOMAIN')} SET IS_ACTIVE = FALSE WHERE DOMAIN_ID = ?", [domain["DOMAIN_ID"]])
    repo.audit(session, "DOMAIN_DROPPED", "DOMAIN", domain["DOMAIN_ID"], domain["DOMAIN_ID"], {"path": domain["PATH"]})
    return {"dropped": domain["PATH"]}


# =========================================================================
# Concepts
# =========================================================================


def _propose_concept(session, domain_path, name, kind, definition, synonyms=None, formula=None,
                     exclusions=None, formula_provenance=None, scope=None, source=None, source_ref=None):
    domain = repo.get_domain(session, normalize_domain_path(domain_path))
    user = authz.require_domain_privilege(session, domain, "SUGGEST")
    p = validate_concept(name, kind, definition, synonyms, formula, exclusions, formula_provenance, scope)
    source = validate_source(source)
    qn = qualified_name(domain["PATH"], p.name, p.scope)
    if repo.find_concept(session, qn):
        raise Conflict(f"concept '{qn}' already exists; propose an edit or use a scope")
    concept_id, version_id = _uuid(session), _uuid(session)
    repo.execute(
        session,
        f"""INSERT INTO {repo.t(session, 'CONCEPT')}
            (CONCEPT_ID, DOMAIN_ID, NAME, SCOPE, KIND, QUALIFIED_NAME, STATUS, PENDING_VERSION_ID, CREATED_BY, CREATED_AT, UPDATED_AT)
            SELECT ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()""",
        [concept_id, domain["DOMAIN_ID"], p.name, p.scope, p.kind, qn, version_id, user],
    )
    _insert_version(session, version_id, concept_id, domain["DOMAIN_ID"], 1, "DRAFT", p.as_version_dict(),
                    source, source_ref, None, "Initial proposal", user)
    repo.audit(session, "CONCEPT_PROPOSED", "CONCEPT", concept_id, domain["DOMAIN_ID"],
               {"qualified_name": qn, "kind": p.kind, "source": source, "source_ref": source_ref})
    return {"concept_id": concept_id, "version_id": version_id, "qualified_name": qn, "status": "DRAFT"}


def _insert_version(session, version_id, concept_id, domain_id, version_no, status, f, source, source_ref,
                    base_version_id, note, author):
    repo.execute(
        session,
        f"""INSERT INTO {repo.t(session, 'CONCEPT_VERSION')}
            (VERSION_ID, CONCEPT_ID, DOMAIN_ID, VERSION_NO, STATUS, DEFINITION, SYNONYMS, FORMULA, EXCLUSIONS,
             FORMULA_PROVENANCE, SOURCE, SOURCE_REF, BASE_VERSION_ID, CHANGE_NOTE, AUTHOR, CREATED_AT)
            SELECT ?, ?, ?, ?, ?, ?, PARSE_JSON(?), ?, PARSE_JSON(?), ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP()""",
        [version_id, concept_id, domain_id, version_no, status, f["definition"], repo.jparam(f["synonyms"]),
         f["formula"], repo.jparam(f["exclusions"]), f["formula_provenance"], source, source_ref,
         base_version_id, note, author],
    )


@handler
def propose_concept(session, domain_path, name, kind, definition, synonyms=None, formula=None,
                    exclusions=None, formula_provenance=None, scope=None, source=None, source_ref=None):
    return _propose_concept(session, domain_path, name, kind, definition, synonyms, formula, exclusions,
                            formula_provenance, scope, source, source_ref)


@handler
def propose_edit(session, qname, changes, note=None):
    c = repo.get_concept(session, qname)
    domain = repo.get_domain_by_id(session, c["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "SUGGEST")
    if isinstance(changes, str):
        changes = json.loads(changes)
    latest = repo.latest_version(session, c["CONCEPT_ID"])
    if c["STATUS"] == "DRAFT":
        # Unapproved drafts are revised in place by their author (or a steward).
        if latest["AUTHOR"].upper() != user.upper() and not authz.has_domain_privilege(session, domain["DOMAIN_ID"], "MODIFY", user):
            raise NotAuthorized("only the author or a domain steward can revise a draft")
        nxt = apply_edit(c["KIND"], repo.version_fields(latest), changes)
        repo.execute(
            session,
            f"""UPDATE {repo.t(session, 'CONCEPT_VERSION')}
                   SET DEFINITION=?, SYNONYMS=PARSE_JSON(?), FORMULA=?, EXCLUSIONS=PARSE_JSON(?), FORMULA_PROVENANCE=?,
                       CHANGE_NOTE=COALESCE(?, CHANGE_NOTE)
                 WHERE VERSION_ID = ?""",
            [nxt["definition"], repo.jparam(nxt["synonyms"]), nxt["formula"], repo.jparam(nxt["exclusions"]),
             nxt["formula_provenance"], note, latest["VERSION_ID"]],
        )
        repo.audit(session, "DRAFT_REVISED", "CONCEPT", c["CONCEPT_ID"], c["DOMAIN_ID"], {"changes": changes})
        return {"concept_id": c["CONCEPT_ID"], "version_id": latest["VERSION_ID"], "status": "DRAFT"}

    if c.get("PENDING_VERSION_ID"):
        raise Conflict(f"'{qname}' already has a pending edit ({c['PENDING_VERSION_ID']}); approve or reject it first")
    current = repo.get_version(session, c["CURRENT_VERSION_ID"])
    nxt = apply_edit(c["KIND"], repo.version_fields(current), changes)
    version_id = _uuid(session)
    _insert_version(session, version_id, c["CONCEPT_ID"], c["DOMAIN_ID"], latest["VERSION_NO"] + 1, "PROPOSED",
                    nxt, "MANUAL", None, current["VERSION_ID"], note, user)
    repo.execute(session, f"UPDATE {repo.t(session, 'CONCEPT')} SET PENDING_VERSION_ID=?, UPDATED_AT=CURRENT_TIMESTAMP() WHERE CONCEPT_ID=?",
                 [version_id, c["CONCEPT_ID"]])
    repo.audit(session, "EDIT_PROPOSED", "CONCEPT", c["CONCEPT_ID"], c["DOMAIN_ID"],
               {"version_id": version_id, "changes": changes, "note": note})
    return {"concept_id": c["CONCEPT_ID"], "version_id": version_id, "status": "PROPOSED",
            "note": "the approved version remains authoritative until a steward approves this edit"}


def _review_version(session, version_id, decision, note):
    v = repo.get_version(session, version_id)
    if v["STATUS"] not in ("DRAFT", "PROPOSED"):
        raise Conflict(f"version {version_id} is {v['STATUS']}, not awaiting review")
    c = repo.get_concept_by_id(session, v["CONCEPT_ID"])
    domain = repo.get_domain_by_id(session, c["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "MODIFY")
    if decision == "APPROVE":
        _require_distinct_approver(session, v["AUTHOR"], user)
        if v["STATUS"] == "PROPOSED" and v["BASE_VERSION_ID"] != c["CURRENT_VERSION_ID"]:
            raise Conflict("proposed edit is stale: the approved version changed since it was proposed")
        repo.execute(session, f"""UPDATE {repo.t(session, 'CONCEPT_VERSION')} SET STATUS='SUPERSEDED'
                                    WHERE CONCEPT_ID=? AND STATUS='APPROVED'""", [c["CONCEPT_ID"]])
        new_status = "APPROVED"
        repo.execute(session, f"""UPDATE {repo.t(session, 'CONCEPT')}
                                     SET STATUS='APPROVED', CURRENT_VERSION_ID=?, PENDING_VERSION_ID=NULL,
                                         UPDATED_AT=CURRENT_TIMESTAMP()
                                   WHERE CONCEPT_ID=?""", [version_id, c["CONCEPT_ID"]])
    else:
        new_status = "REJECTED"
        if c["STATUS"] == "DRAFT":
            repo.execute(session, f"UPDATE {repo.t(session, 'CONCEPT')} SET STATUS='REJECTED', PENDING_VERSION_ID=NULL, UPDATED_AT=CURRENT_TIMESTAMP() WHERE CONCEPT_ID=?",
                         [c["CONCEPT_ID"]])
        else:
            repo.execute(session, f"UPDATE {repo.t(session, 'CONCEPT')} SET PENDING_VERSION_ID=NULL, UPDATED_AT=CURRENT_TIMESTAMP() WHERE CONCEPT_ID=?",
                         [c["CONCEPT_ID"]])
    repo.execute(session, f"""UPDATE {repo.t(session, 'CONCEPT_VERSION')}
                                 SET STATUS=?, REVIEWED_BY=?, REVIEWED_AT=CURRENT_TIMESTAMP(), REVIEW_NOTE=?
                               WHERE VERSION_ID=?""", [new_status, user, note, version_id])
    repo.audit(session, f"VERSION_{new_status}", "CONCEPT", c["CONCEPT_ID"], c["DOMAIN_ID"],
               {"version_id": version_id, "version_no": v["VERSION_NO"], "author": v["AUTHOR"], "note": note})
    return {"qualified_name": c["QUALIFIED_NAME"], "version_id": version_id, "version_no": v["VERSION_NO"], "status": new_status}


@handler
def approve_version(session, version_id, note=None):
    return _review_version(session, version_id, "APPROVE", note)


@handler
def reject_version(session, version_id, note=None):
    if not note:
        raise ValidationError("a rejection note is required so the author knows what to fix")
    return _review_version(session, version_id, "REJECT", note)


@handler
def merge_concepts(session, survivor_qname, duplicate_qname, note=None):
    s = repo.get_concept(session, survivor_qname)
    d = repo.get_concept(session, duplicate_qname)
    if s["DOMAIN_ID"] != d["DOMAIN_ID"]:
        raise ValidationError("merge only within a domain; link cross-domain concepts with EQUIVALENT_TO")
    if s["CONCEPT_ID"] == d["CONCEPT_ID"]:
        raise ValidationError("cannot merge a concept into itself")
    if s["KIND"] != d["KIND"]:
        raise ValidationError(f"cannot merge a {d['KIND']} into a {s['KIND']}")
    if s["STATUS"] != "APPROVED":
        raise ValidationError("the surviving concept must be approved")
    domain = repo.get_domain_by_id(session, s["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "MODIFY")
    T = lambda n: repo.t(session, n)  # noqa: E731
    sid, did = s["CONCEPT_ID"], d["CONCEPT_ID"]
    # Re-point edges, dropping those that would become self-loops or duplicates.
    for col, other in (("SOURCE_CONCEPT_ID", "TARGET_CONCEPT_ID"), ("TARGET_CONCEPT_ID", "SOURCE_CONCEPT_ID")):
        repo.execute(session, f"""UPDATE {T('RELATIONSHIP')} R SET STATUS='RETIRED'
                                   WHERE R.{col}=? AND (R.{other}=? OR EXISTS (
                                     SELECT 1 FROM {T('RELATIONSHIP')} X WHERE X.{col}=? AND X.{other}=R.{other}
                                       AND X.REL_TYPE=R.REL_TYPE AND EQUAL_NULL(X.CUSTOM_LABEL,R.CUSTOM_LABEL)
                                       AND X.STATUS IN ('DRAFT','APPROVED')))""", [did, sid, sid])
        repo.execute(session, f"UPDATE {T('RELATIONSHIP')} SET {col}=? WHERE {col}=? AND STATUS IN ('DRAFT','APPROVED')", [sid, did])
    repo.execute(session, f"UPDATE {T('REPRESENTATION')} SET CONCEPT_ID=? WHERE CONCEPT_ID=? AND STATUS IN ('DRAFT','APPROVED')", [sid, did])
    # Survivor gains the duplicate's name and synonyms as a new approved version.
    cur = repo.version_fields(repo.get_version(session, s["CURRENT_VERSION_ID"]))
    dup_fields = repo.version_fields(repo.latest_version(session, did))
    syn = cur["synonyms"] + [x for x in [d["NAME"]] + dup_fields["synonyms"]
                             if x.lower() not in {y.lower() for y in cur["synonyms"] + [s["NAME"]]}]
    if syn != cur["synonyms"]:
        cur["synonyms"] = syn
        latest_no = repo.latest_version(session, sid)["VERSION_NO"]
        vid = _uuid(session)
        repo.execute(session, f"UPDATE {T('CONCEPT_VERSION')} SET STATUS='SUPERSEDED' WHERE CONCEPT_ID=? AND STATUS='APPROVED'", [sid])
        _insert_version(session, vid, sid, s["DOMAIN_ID"], latest_no + 1, "APPROVED", cur, "MANUAL", None,
                        s["CURRENT_VERSION_ID"], f"Merged {d['QUALIFIED_NAME']}", user)
        repo.execute(session, f"UPDATE {T('CONCEPT_VERSION')} SET REVIEWED_BY=?, REVIEWED_AT=CURRENT_TIMESTAMP() WHERE VERSION_ID=?", [user, vid])
        repo.execute(session, f"UPDATE {T('CONCEPT')} SET CURRENT_VERSION_ID=?, UPDATED_AT=CURRENT_TIMESTAMP() WHERE CONCEPT_ID=?", [vid, sid])
    repo.execute(session, f"""UPDATE {T('CONCEPT')} SET STATUS='MERGED', MERGED_INTO=?, PENDING_VERSION_ID=NULL,
                               UPDATED_AT=CURRENT_TIMESTAMP() WHERE CONCEPT_ID=?""", [sid, did])
    repo.audit(session, "CONCEPT_MERGED", "CONCEPT", did, s["DOMAIN_ID"],
               {"survivor": s["QUALIFIED_NAME"], "duplicate": d["QUALIFIED_NAME"], "note": note})
    return {"survivor": s["QUALIFIED_NAME"], "merged": d["QUALIFIED_NAME"]}


@handler
def retire_concept(session, qname, note=None):
    c = repo.get_concept(session, qname)
    domain = repo.get_domain_by_id(session, c["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "MODIFY")
    if not note:
        raise ValidationError("a retirement note is required")
    cid = c["CONCEPT_ID"]
    repo.execute(session, f"UPDATE {repo.t(session, 'CONCEPT')} SET STATUS='RETIRED', PENDING_VERSION_ID=NULL, UPDATED_AT=CURRENT_TIMESTAMP() WHERE CONCEPT_ID=?", [cid])
    repo.execute(session, f"UPDATE {repo.t(session, 'RELATIONSHIP')} SET STATUS='RETIRED' WHERE (SOURCE_CONCEPT_ID=? OR TARGET_CONCEPT_ID=?) AND STATUS IN ('DRAFT','APPROVED')", [cid, cid])
    repo.execute(session, f"UPDATE {repo.t(session, 'REPRESENTATION')} SET STATUS='RETIRED' WHERE CONCEPT_ID=? AND STATUS IN ('DRAFT','APPROVED')", [cid])
    # Findings on links that no longer exist are moot; close them so health metrics stay meaningful.
    repo.execute(session, f"""UPDATE {repo.t(session, 'DRIFT_FINDING')} SET RESOLVED_AT=CURRENT_TIMESTAMP(), RESOLVED_BY=?,
                               RESOLUTION='RETIRE_LINK', RESOLUTION_NOTE='concept retired' WHERE CONCEPT_ID=? AND RESOLVED_AT IS NULL""",
                 [user, cid])
    repo.audit(session, "CONCEPT_RETIRED", "CONCEPT", cid, c["DOMAIN_ID"], {"note": note, "by": user})
    return {"retired": c["QUALIFIED_NAME"]}


# =========================================================================
# Relationships and representations
# =========================================================================


def _propose_relationship(session, source_qname, rel_type, target_qname, custom_label=None, note=None):
    s = repo.get_concept(session, source_qname)
    tgt = repo.get_concept(session, target_qname)
    domain = repo.get_domain_by_id(session, s["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "SUGGEST")
    if not authz.has_domain_privilege(session, tgt["DOMAIN_ID"], "READ", user):
        raise NotFound(f"concept '{target_qname}' does not exist or is not visible to you")
    rel_type, label = validate_relationship(
        rel_type,
        {"concept_id": s["CONCEPT_ID"], "domain_id": s["DOMAIN_ID"], "kind": s["KIND"]},
        {"concept_id": tgt["CONCEPT_ID"], "domain_id": tgt["DOMAIN_ID"], "kind": tgt["KIND"]},
        custom_label,
    )
    T = repo.t(session, "RELATIONSHIP")
    dup = repo.one(session, f"""SELECT REL_ID FROM {T} WHERE SOURCE_CONCEPT_ID=? AND TARGET_CONCEPT_ID=? AND REL_TYPE=?
                                 AND EQUAL_NULL(CUSTOM_LABEL, ?) AND STATUS IN ('DRAFT','APPROVED')""",
                   [s["CONCEPT_ID"], tgt["CONCEPT_ID"], rel_type, label])
    if dup:
        raise Conflict(f"relationship already exists ({dup['REL_ID']})")
    if rel_type == "EQUIVALENT_TO":
        rev = repo.one(session, f"""SELECT REL_ID FROM {T} WHERE SOURCE_CONCEPT_ID=? AND TARGET_CONCEPT_ID=?
                                     AND REL_TYPE='EQUIVALENT_TO' AND STATUS IN ('DRAFT','APPROVED')""",
                       [tgt["CONCEPT_ID"], s["CONCEPT_ID"]])
        if rev:
            raise Conflict("EQUIVALENT_TO is symmetric and already exists in the other direction")
    if rel_type in ACYCLIC_TYPES:
        edges = [(r["SOURCE_CONCEPT_ID"], r["TARGET_CONCEPT_ID"]) for r in repo.rows(
            session, f"SELECT SOURCE_CONCEPT_ID, TARGET_CONCEPT_ID FROM {T} WHERE REL_TYPE=? AND STATUS IN ('DRAFT','APPROVED')", [rel_type])]
        if would_create_cycle(edges, s["CONCEPT_ID"], tgt["CONCEPT_ID"]):
            raise ValidationError(f"{rel_type} would create a cycle")
    rel_id = _uuid(session)
    repo.execute(session, f"""INSERT INTO {T} (REL_ID, SOURCE_CONCEPT_ID, TARGET_CONCEPT_ID, REL_TYPE, CUSTOM_LABEL,
                               DOMAIN_ID, STATUS, NOTE, CREATED_BY, CREATED_AT)
                              SELECT ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, CURRENT_TIMESTAMP()""",
                 [rel_id, s["CONCEPT_ID"], tgt["CONCEPT_ID"], rel_type, label, s["DOMAIN_ID"], note, user])
    repo.audit(session, "RELATIONSHIP_PROPOSED", "RELATIONSHIP", rel_id, s["DOMAIN_ID"],
               {"source": s["QUALIFIED_NAME"], "type": rel_type, "label": label, "target": tgt["QUALIFIED_NAME"]})
    return {"rel_id": rel_id, "status": "DRAFT", "edge": f"{s['QUALIFIED_NAME']} -{label or rel_type}-> {tgt['QUALIFIED_NAME']}"}


@handler
def propose_relationship(session, source_qname, rel_type, target_qname, custom_label=None, note=None):
    return _propose_relationship(session, source_qname, rel_type, target_qname, custom_label, note)


def _propose_representation(session, qname, rep_type, asset_kind, asset_fqn, asset_member=None, note=None):
    c = repo.get_concept(session, qname)
    domain = repo.get_domain_by_id(session, c["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "SUGGEST")
    rep_type, asset_kind, asset_fqn, asset_member = validate_representation(rep_type, asset_kind, asset_fqn, asset_member)
    snap = drift.snapshot(session, asset_kind, asset_fqn, asset_member)
    if snap is None:
        raise NotFound(f"{asset_kind} {asset_fqn}{'#' + asset_member if asset_member else ''} does not exist "
                       "or is not visible to the ontology service role (grant it REFERENCES/SELECT)")
    T = repo.t(session, "REPRESENTATION")
    if repo.one(session, f"""SELECT 1 AS X FROM {T} WHERE CONCEPT_ID=? AND REP_TYPE=? AND ASSET_KIND=? AND ASSET_FQN=?
                             AND EQUAL_NULL(ASSET_MEMBER, ?) AND STATUS IN ('DRAFT','APPROVED')""",
                [c["CONCEPT_ID"], rep_type, asset_kind, asset_fqn, asset_member]):
        raise Conflict("representation already exists")
    rep_id = _uuid(session)
    repo.execute(session, f"""INSERT INTO {T} (REP_ID, CONCEPT_ID, DOMAIN_ID, REP_TYPE, ASSET_KIND, ASSET_FQN, ASSET_MEMBER,
                               STATUS, LAST_SNAPSHOT, ASSET_FINGERPRINT, VALIDATION_STATUS, LAST_VALIDATED_AT, NOTE, CREATED_BY, CREATED_AT)
                              SELECT ?, ?, ?, ?, ?, ?, ?, 'DRAFT', PARSE_JSON(?), ?, 'VALID', CURRENT_TIMESTAMP(), ?, ?, CURRENT_TIMESTAMP()""",
                 [rep_id, c["CONCEPT_ID"], c["DOMAIN_ID"], rep_type, asset_kind, asset_fqn, asset_member,
                  repo.jparam(snap), fingerprint(snap), note, user])
    repo.audit(session, "REPRESENTATION_PROPOSED", "REPRESENTATION", rep_id, c["DOMAIN_ID"],
               {"concept": c["QUALIFIED_NAME"], "type": rep_type, "asset_kind": asset_kind, "asset": asset_fqn, "member": asset_member})
    return {"rep_id": rep_id, "status": "DRAFT", "snapshot": snap}


@handler
def propose_representation(session, qname, rep_type, asset_kind, asset_fqn, asset_member=None, note=None):
    return _propose_representation(session, qname, rep_type, asset_kind, asset_fqn, asset_member, note)


@handler
def review_link(session, link_kind, link_id, decision, note=None):
    link_kind, decision = (link_kind or "").upper(), (decision or "").upper()
    if link_kind not in ("RELATIONSHIP", "REPRESENTATION"):
        raise ValidationError("link_kind must be RELATIONSHIP or REPRESENTATION")
    if decision not in ("APPROVE", "REJECT"):
        raise ValidationError("decision must be APPROVE or REJECT")
    T = repo.t(session, link_kind)
    key = "REL_ID" if link_kind == "RELATIONSHIP" else "REP_ID"
    link = repo.one(session, f"SELECT * FROM {T} WHERE {key}=?", [link_id])
    if not link:
        raise NotFound(f"{link_kind.lower()} {link_id} not found")
    if link["STATUS"] != "DRAFT":
        raise Conflict(f"{link_kind.lower()} is {link['STATUS']}, not awaiting review")
    domain = repo.get_domain_by_id(session, link["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "MODIFY")
    if decision == "APPROVE":
        _require_distinct_approver(session, link["CREATED_BY"], user)
        ids = [link["SOURCE_CONCEPT_ID"], link["TARGET_CONCEPT_ID"]] if link_kind == "RELATIONSHIP" else [link["CONCEPT_ID"]]
        for cid in ids:
            if repo.get_concept_by_id(session, cid)["STATUS"] != "APPROVED":
                raise Conflict("all linked concepts must be approved before the link can be approved")
    elif not note:
        raise ValidationError("a rejection note is required")
    status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    repo.execute(session, f"UPDATE {T} SET STATUS=?, APPROVED_BY=?, APPROVED_AT=CURRENT_TIMESTAMP(), REVIEW_NOTE=? WHERE {key}=?",
                 [status, user, note, link_id])
    repo.audit(session, f"{link_kind}_{status}", link_kind, link_id, link["DOMAIN_ID"], {"note": note})
    return {link_kind.lower() + "_id": link_id, "status": status}


@handler
def acknowledge_drift(session, finding_id, resolution, note=None):
    """Steward closes a drift finding. ACCEPT re-baselines the snapshot; RETIRE_LINK retires the representation."""
    resolution = (resolution or "").upper()
    if resolution not in ("ACCEPT", "RETIRE_LINK"):
        raise ValidationError("resolution must be ACCEPT or RETIRE_LINK")
    f = repo.one(session, f"SELECT * FROM {repo.t(session, 'DRIFT_FINDING')} WHERE FINDING_ID=? AND RESOLVED_AT IS NULL", [finding_id])
    if not f:
        raise NotFound(f"open finding {finding_id} not found")
    domain = repo.get_domain_by_id(session, f["DOMAIN_ID"])
    user = authz.require_domain_privilege(session, domain, "MODIFY")
    rep = repo.one(session, f"SELECT * FROM {repo.t(session, 'REPRESENTATION')} WHERE REP_ID=?", [f["REP_ID"]])
    if resolution == "ACCEPT":
        snap = drift.snapshot(session, rep["ASSET_KIND"], rep["ASSET_FQN"], rep.get("ASSET_MEMBER"))
        if snap is None:
            raise Conflict("the asset is still missing; use RETIRE_LINK")
        repo.execute(session, f"""UPDATE {repo.t(session, 'REPRESENTATION')} SET LAST_SNAPSHOT=PARSE_JSON(?), ASSET_FINGERPRINT=?,
                                   VALIDATION_STATUS='VALID', LAST_VALIDATED_AT=CURRENT_TIMESTAMP() WHERE REP_ID=?""",
                     [repo.jparam(snap), fingerprint(snap), rep["REP_ID"]])
    else:
        repo.execute(session, f"UPDATE {repo.t(session, 'REPRESENTATION')} SET STATUS='RETIRED' WHERE REP_ID=?", [rep["REP_ID"]])
    repo.execute(session, f"""UPDATE {repo.t(session, 'DRIFT_FINDING')} SET RESOLVED_AT=CURRENT_TIMESTAMP(), RESOLVED_BY=?,
                               RESOLUTION=?, RESOLUTION_NOTE=? WHERE REP_ID=? AND RESOLVED_AT IS NULL""",
                 [user, resolution, note, rep["REP_ID"]])
    repo.audit(session, "DRIFT_RESOLVED", "REPRESENTATION", rep["REP_ID"], f["DOMAIN_ID"], {"resolution": resolution, "note": note})
    return {"rep_id": rep["REP_ID"], "resolution": resolution}


# =========================================================================
# Ingestion paths: Cortex Sense promotion, semantic-view bootstrap, import
# =========================================================================


@handler
def promote_cortex_sense_finding(session, domain_path, finding):
    """Promote an inferred Cortex Sense finding into a DRAFT (never auto-approved)."""
    if isinstance(finding, str):
        finding = json.loads(finding)
    f = {k.lower(): v for k, v in (finding or {}).items()}
    evidence = f.get("evidence") or f.get("source_ref") or f.get("id")
    return _propose_concept(session, domain_path, f.get("name"), f.get("kind", "TERM"), f.get("definition"),
                            f.get("synonyms"), f.get("formula"), f.get("exclusions"),
                            f.get("formula_provenance") or ("cortex_sense" if f.get("formula") else None),
                            f.get("scope"), "CORTEX_SENSE", json.dumps(evidence) if evidence is not None else None)


def _humanize(ident: str) -> str:
    return " ".join(w.capitalize() if not w.isupper() or len(w) > 3 else w for w in re.split(r"[_\s]+", ident.lower()) if w)


@handler
def bootstrap_from_semantic_view(session, semantic_view_fqn, domain_path, include_dimensions=False):
    """Propose DRAFT concepts + IMPLEMENTS representations for each metric (and optionally dimension)."""
    _, _, fqn, _ = validate_representation("IMPLEMENTS", "SEMANTIC_VIEW", semantic_view_fqn)
    members = drift._semantic_members(session, fqn)
    if not members:
        raise NotFound(f"semantic view {fqn} not found or not visible to the ontology service role")
    wanted = {"METRIC": "METRIC", "DERIVED_METRIC": "METRIC"}
    if include_dimensions:
        wanted["DIMENSION"] = "DIMENSION"
    created, skipped = [], []
    for key, m in sorted(members.items()):
        kind = wanted.get(m["object_kind"])
        if not kind:
            continue
        name = _humanize(key.split(".")[-1])
        definition = m.get("comment") or ""
        if len(definition.strip()) < 10:
            definition = f"Proposed from {fqn} {key}; steward must supply the business definition."
        qn = qualified_name(domain_path, name)
        if repo.find_concept(session, qn):
            skipped.append({"name": qn, "reason": "exists"})
            continue
        res = _propose_concept(session, domain_path, name, kind, definition,
                               formula=m.get("expression") if kind == "METRIC" else None,
                               formula_provenance=f"semantic_view:{fqn}#{key}" if kind == "METRIC" else None,
                               source="SEMANTIC_VIEW_BOOTSTRAP", source_ref=f"{fqn}#{key}")
        _propose_representation(session, qn, "IMPLEMENTS", "SV_" + ("METRIC" if kind == "METRIC" else "DIMENSION"),
                                fqn, key, "bootstrap")
        created.append(res["qualified_name"])
    return {"semantic_view": fqn, "created": created, "skipped": skipped}


@handler
def import_glossary(session, stage_path, fmt="AUTO", default_domain=None, dry_run=False):
    from . import importer

    content = importer.read_stage_file(session, stage_path)
    doc_batches = importer.parse(content, fmt, stage_path, default_domain)
    batch_id = _uuid(session)
    report = {"batch_id": batch_id, "dry_run": bool(dry_run), "concepts": [], "relationships": [], "representations": [], "errors": []}

    def attempt(bucket, fn, *a, **kw):
        try:
            report[bucket].append(fn(session, *a, **kw))
        except Exception as e:  # collect per-item errors; never abort the whole batch
            report["errors"].append({"item": bucket, "args": [str(x) for x in a], "error": str(e)})

    for doc in doc_batches:
        for c in doc["concepts"]:
            attempt("concepts", _propose_concept, doc["domain"], c.get("name"), c.get("kind", "TERM"), c.get("definition"),
                    c.get("synonyms"), c.get("formula"), c.get("exclusions"), c.get("formula_provenance"),
                    c.get("scope"), "IMPORT", f"{stage_path}#{c.get('name')}")
    for doc in doc_batches:
        for r in doc["relationships"]:
            attempt("relationships", _propose_relationship, r["source"], r["type"], r["target"], r.get("label"), "import")
        for r in doc["representations"]:
            attempt("representations", _propose_representation, r["concept"], r.get("type", "IMPLEMENTS"),
                    r["asset_kind"], r["asset"], r.get("member"), "import")
    stats = {k: len(report[k]) for k in ("concepts", "relationships", "representations", "errors")}
    if dry_run:
        session.sql("ROLLBACK").collect()
        session.sql("BEGIN").collect()
    repo.execute(session, f"""INSERT INTO {repo.t(session, 'IMPORT_BATCH')} (BATCH_ID, STAGE_PATH, FORMAT, SUBMITTED_BY, SUBMITTED_AT, DRY_RUN, STATS, ERRORS)
                              SELECT ?, ?, ?, CURRENT_USER(), CURRENT_TIMESTAMP(), ?, PARSE_JSON(?), PARSE_JSON(?)""",
                 [batch_id, stage_path, (fmt or "AUTO").upper(), bool(dry_run), repo.jparam(stats), repo.jparam(report["errors"])])
    report["stats"] = stats
    return report


# =========================================================================
# Scheduled (not granted to consumers)
# =========================================================================


def validate_representations(session, domain_path=None):
    authz.reset_cache()
    return drift.validate_all(session, domain_path)
