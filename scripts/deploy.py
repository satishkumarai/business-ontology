#!/usr/bin/env python3
"""Deploy the governed Business Ontology to a Snowflake account.

Usage
-----
  python scripts/deploy.py --env dev                       # full deploy
  python scripts/deploy.py --env prod --connection prod    # named connection
  python scripts/deploy.py --env dev --only 05,06          # selected steps
  python scripts/deploy.py --env dev --dry-run             # render SQL only

Connection resolution: --connection (connections.toml) > SNOWFLAKE_TOKEN_FILE_PATH
(OAuth, e.g. Snowsight sandbox / SPCS) > SNOWFLAKE_ACCOUNT/USER/PRIVATE_KEY_PATH.
All steps are idempotent and safe to re-run.
"""

from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import sys
import time
import zipfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
PKG = ROOT / "src" / "python" / "ontology"

STEPS = [
    ("00", "00_account_bootstrap.sql", "core"),
    ("01", "01_schemas.sql", "core"),
    ("02", "02_core_tables.sql", "core"),
    ("03", "03_policies.sql", "core"),
    ("04", "04_api_views.sql", "core"),
    ("PKG", None, "core"),
    ("05", "05_api_procedures.sql", "core"),
    ("06", "06_api_retrieval.sql", "core"),
    ("07", "07_grants.sql", "core"),
    ("08", "08_ops.sql", "core"),
    ("09", "09_sample_data.sql", "sample"),
    ("10", "10_sample_semantic_views.sql", "sample"),
    ("11", "11_agent.sql", "agent"),
    ("07b", "07_grants.sql", "core"),  # re-apply grants to objects created after 07
]

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def load_env(name: str) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "environments.yml").read_text())
    if name not in cfg["environments"]:
        sys.exit(f"unknown environment '{name}'; choose from {list(cfg['environments'])}")
    env = {**cfg.get("defaults", {}), **cfg["environments"][name], "ENV": name}
    for k in ("DB", "WAREHOUSE", "ADMIN_ROLE", "CONSUMER_ROLE", "ROLE_PREFIX"):
        if not _IDENT.match(str(env[k])):
            sys.exit(f"config {k}={env[k]!r} is not a valid unquoted identifier")
    return env


def render(sql: str, env: dict) -> str:
    def sub(m):
        key = m.group(1)
        if key not in env:
            raise KeyError(f"template variable {{{{{key}}}}} not defined in environment")
        return str(env[key])

    return _PLACEHOLDER.sub(sub, sql)


def connect(args):
    import snowflake.connector

    if args.connection:
        return snowflake.connector.connect(connection_name=args.connection)
    token_file = os.environ.get("SNOWFLAKE_TOKEN_FILE_PATH")
    if token_file and os.path.exists(token_file):
        return snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            host=os.environ.get("SNOWFLAKE_HOST"),
            authenticator="oauth",
            token=open(token_file).read().strip(),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        )
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        authenticator="SNOWFLAKE_JWT",
        private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
    )


def build_package(env: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(PKG.rglob("*.py")):
            if f.name != "_deployment.py":
                z.write(f, f.relative_to(PKG.parent).as_posix())
        # Caller's-rights procedures cannot infer their own database from the
        # caller's session, so the target database is stamped in at build time.
        z.writestr("ontology/_deployment.py", f'DB = "{env["DB"]}"\nENV = "{env["ENV"]}"\n')
    return buf.getvalue()


def upload_package(cur, env: dict) -> None:
    tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "ontology_build"
    tmp.mkdir(parents=True, exist_ok=True)
    zpath = tmp / "ontology.zip"
    zpath.write_bytes(build_package(env))
    cur.execute(f"USE ROLE {env['ADMIN_ROLE']}")
    cur.execute(
        f"PUT 'file://{zpath.as_posix()}' @{env['DB']}.CORE.CODE_STAGE AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
    )


def run_sql(conn, sql: str, label: str) -> None:
    for cur in conn.execute_string(sql, remove_comments=True):
        try:
            row = cur.fetchone()
        except Exception:
            row = None
        if row and isinstance(row[0], str) and label and ("policies" in row[0] or "status" in row[0].lower()):
            print(f"      {row[0]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="dev")
    ap.add_argument("--connection")
    ap.add_argument("--only", help="comma-separated step ids, e.g. 05,06,PKG")
    ap.add_argument("--skip-bootstrap", action="store_true", help="skip step 00 (no ACCOUNTADMIN available)")
    ap.add_argument("--dry-run", action="store_true", help="print rendered SQL and exit")
    args = ap.parse_args()

    env = load_env(args.env)
    only = set(args.only.split(",")) if args.only else None
    groups = {"core"} | ({"sample"} if env.get("include_sample") else set()) | ({"agent"} if env.get("include_agent") else set())
    plan = [s for s in STEPS if s[2] in groups and (only is None or s[0] in only or s[0].rstrip("b") in only)]
    if args.skip_bootstrap:
        plan = [s for s in plan if s[0] != "00"]

    if args.dry_run:
        for sid, fname, _ in plan:
            print(f"-- ===== step {sid}: {fname or 'python package upload'}")
            if fname:
                print(render((DEPLOY / fname).read_text(), env))
        return

    conn = connect(args)
    cur = conn.cursor()
    print(f"Deploying Business Ontology [{args.env}] -> {env['DB']}")
    for sid, fname, _ in plan:
        t0 = time.time()
        label = fname or "python package -> CORE.CODE_STAGE"
        print(f"  [{sid:>3}] {label} ...", flush=True)
        if fname is None:
            upload_package(cur, env)
        else:
            run_sql(conn, render((DEPLOY / fname).read_text(), env), label)
        print(f"        ok ({time.time() - t0:.1f}s)")
    print("Deployment complete.")


if __name__ == "__main__":
    main()
