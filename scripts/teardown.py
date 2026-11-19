#!/usr/bin/env python3
"""Tear down ontology data or the whole deployment.

  python scripts/teardown.py --env dev --data      # wipe concepts/domains + domain roles, keep objects
  python scripts/teardown.py --env dev --all       # drop database, managed roles, demo personas

Refuses to touch an environment whose include_sample is false (i.e. prod)
unless --force-prod is passed.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from deploy import connect, load_env  # noqa: E402
import personas  # noqa: E402

DATA_TABLES = ["DRIFT_FINDING", "REPRESENTATION", "RELATIONSHIP", "CONCEPT_VERSION", "CONCEPT",
               "DOMAIN_ANCESTOR", "DOMAIN_ACCESS", "DOMAIN", "IMPORT_BATCH", "AUDIT_EVENT"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--connection")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--data", action="store_true")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--force-prod", action="store_true")
    args = ap.parse_args()
    env = load_env(args.env)
    if not env.get("include_sample") and not args.force_prod:
        sys.exit(f"refusing to tear down '{args.env}' (looks like prod); pass --force-prod to override")

    cur = connect(args).cursor()
    cur.execute(f"USE ROLE {env['ADMIN_ROLE']}")
    db = env["DB"]
    roles = [r[0] for r in cur.execute(f"SELECT DISTINCT ROLE_NAME FROM {db}.CORE.DOMAIN_ACCESS WHERE IS_MANAGED").fetchall()]
    for r in roles:
        cur.execute(f'DROP ROLE IF EXISTS "{r}"')
    print(f"dropped {len(roles)} managed domain roles")
    if args.data:
        for t in DATA_TABLES:
            cur.execute(f"DELETE FROM {db}.CORE.{t}")
        cur.execute(f"REMOVE @{db}.API.IMPORT_STAGE")
        print("ontology data wiped")
        return
    cur.execute("USE ROLE ACCOUNTADMIN")
    cur.execute(f"DROP DATABASE IF EXISTS {db}")
    personas.drop_personas(cur.connection)
    cur.execute(f"DROP ROLE IF EXISTS {env['CONSUMER_ROLE']}")
    cur.execute(f"DROP ROLE IF EXISTS {env['ADMIN_ROLE']}")
    print(f"dropped {db}, personas, {env['CONSUMER_ROLE']}, {env['ADMIN_ROLE']}")


if __name__ == "__main__":
    main()
