#!/usr/bin/env bash
# One-shot deployment of the governed Business Ontology.
#
#   ./deploy.sh                      # dev: deploy + seed demo + smoke test
#   ./deploy.sh --env prod           # prod: deploy only (no demo seed)
#   ./deploy.sh --test               # also run integration + agent tests
#   ./deploy.sh --connection myconn  # use a connections.toml entry
#   ./deploy.sh --skip-bootstrap     # step 00 already applied by a platform admin
#
# Connection: --connection > SNOWFLAKE_TOKEN_FILE_PATH (OAuth) > key-pair env vars
# (SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_PATH). Safe to re-run.
set -euo pipefail

ENV_NAME=dev
CONN_ARGS=()
DEPLOY_ARGS=()
RUN_TESTS=0
SEED=auto

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)            ENV_NAME="$2"; shift 2 ;;
    --connection)     CONN_ARGS=(--connection "$2"); export ONTOLOGY_CONNECTION="$2"; shift 2 ;;
    --skip-bootstrap) DEPLOY_ARGS+=(--skip-bootstrap); shift ;;
    --test)           RUN_TESTS=1; shift ;;
    --no-seed)        SEED=no; shift ;;
    -h|--help)        sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
export ONTOLOGY_ENV="$ENV_NAME"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Checking dependencies"
"$PY" - <<'EOF' 2>/dev/null || "$PY" -m pip install -q -r requirements.txt
import snowflake.connector, yaml, cryptography, pytest  # noqa
EOF

step "Unit tests (offline)"
"$PY" -m pytest tests/unit -q

step "Deploying [$ENV_NAME]"
"$PY" scripts/deploy.py --env "$ENV_NAME" "${CONN_ARGS[@]}" "${DEPLOY_ARGS[@]}"

INCLUDE_SAMPLE=$("$PY" -c "import sys; sys.path.insert(0,'scripts'); import deploy; print(bool(deploy.load_env('$ENV_NAME').get('include_sample')))")
if [[ "$SEED" == auto && "$INCLUDE_SAMPLE" == True ]]; then
  step "Seeding demo domains through the governed API"
  "$PY" scripts/seed_demo.py --env "$ENV_NAME" "${CONN_ARGS[@]}"
fi

step "Smoke test"
"$PY" - <<EOF
import json, os, sys
sys.path.insert(0, "scripts")
import deploy
env = deploy.load_env("$ENV_NAME")
conn = deploy.connect(type("A", (), {"connection": os.environ.get("ONTOLOGY_CONNECTION")})())
cur = conn.cursor()
cur.execute(f"USE ROLE {env['ADMIN_ROLE']}")
cur.execute(f"USE WAREHOUSE {env['WAREHOUSE']}")
db = env["DB"]
checks = {
    "procedures": f"SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA = 'API'",
    "views":      f"SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA = 'API'",
    "concepts":   f"SELECT COUNT(*) FROM {db}.API.V_CONCEPT",
}
res = {k: cur.execute(q).fetchone()[0] for k, q in checks.items()}
cur.execute(f"SHOW TASKS LIKE 'T_VALIDATE_REPRESENTATIONS' IN SCHEMA {db}.OPS")
res["drift_task"] = cur.fetchone()[cur.description.index(next(d for d in cur.description if d[0] == 'state'))]
health = cur.execute(f"SELECT OBJECT_CONSTRUCT(*) FROM {db}.OPS.V_HEALTH").fetchone()[0]
print(json.dumps(res, indent=2)); print("health:", health)
assert res["procedures"] >= 19 and res["views"] >= 9, "API objects missing"
assert res["drift_task"] == "started", "drift task not running"
if env.get("include_sample"):
    assert res["concepts"] >= 13, "demo concepts missing"
    s = json.loads(cur.execute(f"CALL {db}.API.SEARCH_CONCEPTS('net revenue')").fetchone()[0])
    assert {"Finance.Net Revenue", "Sales.Net Revenue"} <= {r["qualified_name"] for r in s["results"]}
    print("search: ambiguity detected ->", s["ambiguous_terms"])
print("SMOKE TEST PASSED")
EOF

if [[ "$RUN_TESTS" == 1 ]]; then
  step "Integration tests (slow)"
  "$PY" -m pytest tests/integration -q
  step "Agent evaluation (calls Cortex)"
  "$PY" -m pytest tests/agent -q
fi

step "Done. Try it: Snowsight > AI & ML > Agents > ONTOLOGY_AGENT (env: $ENV_NAME)"
