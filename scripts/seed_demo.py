#!/usr/bin/env python3
"""Seed the demo ontology through the *governed* path, using separate personas.

  admin          creates domains Finance, Sales, Finance.EMEA and grants privileges
  author         uploads glossaries and imports them (DRAFTs)
  fin steward    approves Finance / Finance.EMEA concepts and links
  sales steward  approves Sales concepts and links
  admin          runs the first representation validation

Idempotent: re-running skips existing items.
Usage: python scripts/seed_demo.py --env dev
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from deploy import ROOT, connect, load_env  # noqa: E402
import personas  # noqa: E402
from actors import Harness  # noqa: E402

GLOSSARIES = [
    ("finance.yaml", None),
    ("sales.yaml", None),
    ("finance_emea.ttl", "Finance.EMEA"),
]


def call(cur, sql: str, params=None):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return json.loads(row[0]) if row and isinstance(row[0], str) else (row[0] if row else None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--connection")
    args = ap.parse_args()
    env = load_env(args.env)
    db = env["DB"]

    admin = connect(args)
    h = Harness(admin, env)
    print(f"actor mode: {h.mode}")
    personas.ensure_personas(admin, env, create_users=(h.mode == "users"))
    if h.mode == "roles" and env.get("REQUIRE_DISTINCT_APPROVER", "TRUE").upper() == "TRUE":
        sys.exit("single-identity (roles) mode cannot satisfy REQUIRE_DISTINCT_APPROVER=TRUE; "
                 "run with key-pair personas (users mode) or use an environment that sets it FALSE")
    cur = h.admin_cursor()
    cur.execute(f"USE ROLE {env['ADMIN_ROLE']}")
    cur.execute(f"USE WAREHOUSE {env['WAREHOUSE']}")

    print("== domains")
    existing = {r[0] for r in cur.execute(f"SELECT PATH FROM {db}.CORE.DOMAIN WHERE IS_ACTIVE").fetchall()}
    for path, desc, steward in personas.DOMAINS:
        if path in existing:
            print(f"   exists  {path}")
            continue
        res = call(cur, f"CALL {db}.API.CREATE_DOMAIN(%s, %s, NULL, %s)", (path, desc, steward))
        print(f"   created {res['path']}")
    for persona, (role, privs) in personas.PERSONAS.items():
        for path, priv in privs.items():
            call(cur, f"CALL {db}.API.SET_DOMAIN_PRIVILEGE(%s, %s, %s, 'GRANT')", (path, priv, role))
    print("   privileges granted")

    print("== import (author)")
    author = h.actor("AUTHOR")
    for fname, default_domain in GLOSSARIES:
        local = (ROOT / "seed" / "glossary" / fname).as_posix()
        author.put(local, f"@{db}.API.IMPORT_STAGE/seed/")
        res = author.call(f"CALL {db}.API.IMPORT_GLOSSARY(%s, 'AUTO', %s, FALSE)",
                          (f"@{db}.API.IMPORT_STAGE/seed/{fname}", default_domain))
        conflicts = [e for e in res["errors"] if "CONFLICT" in e["error"]]
        other = [e for e in res["errors"] if "CONFLICT" not in e["error"]]
        print(f"   {fname}: {res['stats']}  (existing skipped: {len(conflicts)})")
        for e in other:
            print(f"     ! {e['item']}: {e['error']}")

    print("== review (stewards)")
    for persona in ("FIN_STEWARD", "SALES_STEWARD"):
        sc = h.actor(persona)
        # Concepts first: links can only be approved once both ends are approved.
        for item_type in ("CONCEPT_VERSION", "RELATIONSHIP", "REPRESENTATION"):
            items = sc.query(
                f"SELECT ITEM_ID, SUBJECT FROM {db}.API.V_REVIEW_QUEUE WHERE ITEM_TYPE = %s ORDER BY CREATED_AT", (item_type,)
            )
            for item_id, subject in items:
                if item_type == "CONCEPT_VERSION":
                    sc.call(f"CALL {db}.API.APPROVE_VERSION(%s, 'Seed review: matches the approved glossary')", (item_id,))
                else:
                    sc.call(f"CALL {db}.API.REVIEW_LINK(%s, %s, 'APPROVE', 'Seed review')", (item_type, item_id))
                print(f"   {persona:<13} approved {item_type.lower():<15} {subject}")

    print("== validate representations")
    cur = h.admin_cursor()
    cur.execute(f"USE ROLE {env['ADMIN_ROLE']}")
    print("  ", call(cur, f"CALL {db}.CORE.VALIDATE_REPRESENTATIONS()"))
    h.close()
    print("== done")


if __name__ == "__main__":
    main()
