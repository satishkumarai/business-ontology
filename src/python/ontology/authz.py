"""Authorization for the governed write path.

Write procedures run with OWNER's rights so callers never need DML on CORE
tables. Inside an owner's-rights procedure Snowflake evaluates role functions
(IS_ROLE_IN_SESSION, CURRENT_ROLE) against the *owner*, but CURRENT_USER()
still returns the real caller. We therefore authorize by walking the role
graph upward from each domain privilege role (SHOW GRANTS OF ROLE) until we
reach the calling user.

Semantics: a user is authorized if the privilege role is *granted* to them,
directly or through any role hierarchy. Privilege roles are themselves nested
MODIFY -> SUGGEST -> READ -> USAGE, so holding MODIFY implies the rest.
"""

from __future__ import annotations

from typing import Optional

from . import repo
from .validation import NotAuthorized

_MAX_ROLES_VISITED = 500
_cache: dict[tuple[str, str], bool] = {}


def reset_cache() -> None:
    _cache.clear()


def _quote(role: str) -> str:
    return '"' + role.replace('"', '""') + '"'


def user_holds_role(session, role: str, user: str) -> bool:
    key = (role.upper(), user.upper())
    if key in _cache:
        return _cache[key]
    frontier, visited, found = [role.upper()], set(), False
    while frontier and len(visited) < _MAX_ROLES_VISITED:
        r = frontier.pop()
        if r in visited:
            continue
        visited.add(r)
        try:
            grants = session.sql(f"SHOW GRANTS OF ROLE {_quote(r)}").collect()
        except Exception:  # role dropped, or not visible to the owner role
            continue
        for g in grants:
            g = g.as_dict()
            kind, grantee = g.get("granted_to"), (g.get("grantee_name") or "").upper()
            if kind == "USER" and grantee == user.upper():
                found = True
                break
            if kind == "ROLE" and grantee not in visited:
                frontier.append(grantee)
        if found:
            break
    _cache[key] = found
    return found


def is_admin(session, user: Optional[str] = None) -> bool:
    user = user or repo.current_user(session)
    admin_role = repo.setting(session, "ADMIN_ROLE", "ONTOLOGY_ADMIN")
    return user_holds_role(session, admin_role, user)


def domain_privilege_roles(session, domain_id: str, privilege: str) -> list[str]:
    rs = repo.rows(
        session,
        f"SELECT ROLE_NAME FROM {repo.t(session, 'DOMAIN_ACCESS')} WHERE DOMAIN_ID = ? AND PRIVILEGE = ?",
        [domain_id, privilege],
    )
    return [r["ROLE_NAME"] for r in rs]


def has_domain_privilege(session, domain_id: str, privilege: str, user: Optional[str] = None) -> bool:
    user = user or repo.current_user(session)
    # Navigation: USAGE on every strict ancestor (no inheritance to children).
    ancestors = repo.rows(
        session,
        f"""SELECT ANCESTOR_ID FROM {repo.t(session, 'DOMAIN_ANCESTOR')}
             WHERE DOMAIN_ID = ? AND DEPTH > 0""",
        [domain_id],
    )
    for a in ancestors:
        if not any(user_holds_role(session, r, user) for r in domain_privilege_roles(session, a["ANCESTOR_ID"], "USAGE")):
            return False
    return any(user_holds_role(session, r, user) for r in domain_privilege_roles(session, domain_id, privilege))


def require_domain_privilege(session, domain: dict, privilege: str) -> str:
    user = repo.current_user(session)
    if not has_domain_privilege(session, domain["DOMAIN_ID"], privilege, user):
        raise NotAuthorized(f"user {user} lacks {privilege} on domain '{domain['PATH']}' (or USAGE on a parent domain)")
    return user


def require_admin(session) -> str:
    user = repo.current_user(session)
    if not is_admin(session, user):
        raise NotAuthorized(f"user {user} is not an ontology administrator")
    return user
