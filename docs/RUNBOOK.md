# Runbook

## Production rollout

### 1. Configure
Edit `config/environments.yml → prod`. Each environment is isolated by database, service role,
consumer role and domain-role prefix, so dev and prod may share an account.
Keep `REQUIRE_DISTINCT_APPROVER: "TRUE"` in prod.

### 2. Bootstrap (once, platform admin)
```bash
python scripts/deploy.py --env prod --only 00 --connection <admin_conn>
```
Grants the service role `CREATE ROLE`, `CREATE DATABASE`, `EXECUTE TASK`, `MANAGE GRANTS`,
`SNOWFLAKE.CORTEX_USER`. Review these with security before running.

### 3. Deploy (CI)
```bash
python scripts/deploy.py --env prod --skip-bootstrap
```
All steps are idempotent; re-run after every change. `--dry-run` prints rendered SQL for review.
The service role must also be able to *see* the assets you link (grant it `REFERENCES` or
`SELECT` on your semantic views / tables), otherwise representation validation reports them missing.

### 4. Create domains and grant access
```sql
CALL ONTOLOGY_PROD.API.CREATE_DOMAIN('Finance', 'Financial reporting', 'FINANCE_OWNER', 'FINANCE_STEWARD');
CALL ONTOLOGY_PROD.API.CREATE_DOMAIN('Finance.EMEA', 'EMEA statutory variants', 'FINANCE_OWNER', 'FINANCE_STEWARD');
CALL ONTOLOGY_PROD.API.SET_DOMAIN_PRIVILEGE('Finance', 'READ', 'ANALYST');
CALL ONTOLOGY_PROD.API.SET_DOMAIN_PRIVILEGE('Finance.EMEA', 'READ', 'EMEA_ANALYST');  -- not inherited
CALL ONTOLOGY_PROD.API.SET_DOMAIN_PRIVILEGE('Finance', 'SUGGEST', 'ANALYTICS_ENGINEER');
```
Remember: to see `Finance.EMEA`, a role needs at least `USAGE` on `Finance` as well.

### 5. Populate
Pick whichever entry point fits; all produce drafts for steward review.
* Existing glossary: `PUT` YAML/JSON/TTL to `@API.IMPORT_STAGE`, `IMPORT_GLOSSARY(..., DRY_RUN => TRUE)` first.
* Semantic views: `BOOTSTRAP_FROM_SEMANTIC_VIEW('<db.schema.sv>', 'Finance')`.
* Cortex Sense: `PROMOTE_CORTEX_SENSE_FINDING('Finance', <finding json>)`.
* By hand: `PROPOSE_CONCEPT` for the handful of terms that cause the most confusion.

Stewards work from `API.V_REVIEW_QUEUE` (see `sql/steward_playbook.sql`).

### 6. Agent
Edit `deploy/11_agent.sql`: replace the demo analyst tools with your semantic views and keep the two
ontology tools. Set `include_agent: true` and redeploy. Grant `USAGE ON AGENT` to business roles
(the consumer role already has it). Run `tests/agent` against prod-like data before announcing.

## Operations

| Check | Query | Healthy |
|---|---|---|
| Overall | `SELECT * FROM OPS.V_HEALTH` | `OPEN_DRIFT_FINDINGS = 0`, `STALE_VALIDATIONS = 0`, recent `LAST_SUCCESSFUL_VALIDATION` |
| Review backlog | `SELECT * FROM OPS.V_STEWARD_WORKLOAD` | `OLDEST_PENDING` within SLA (e.g. 5 business days) |
| Task | `SHOW TASKS IN SCHEMA OPS` | `state = started` |
| Ambiguity | `SELECT * FROM API.V_AMBIGUOUS_TERMS` | Expected duplicates only (e.g. Finance vs Sales Net Revenue) |
| Imports | `SELECT * FROM CORE.IMPORT_BATCH ORDER BY SUBMITTED_AT DESC` | `ERRORS` empty |

Optional e-mail alert on new drift: `deploy/optional/drift_alert.sql`.

### Backup / recovery
`CORE` tables have Time Travel (default retention). For point-in-time recovery of the ontology:
```sql
CREATE DATABASE ONTOLOGY_RESTORE CLONE ONTOLOGY_PROD AT (TIMESTAMP => '<ts>'::TIMESTAMP_LTZ);
```
Raise `DATA_RETENTION_TIME_IN_DAYS` on `CORE` to match your audit requirements. The audit trail plus
append-only versions let you reconstruct any approved definition at any time.

### Upgrades
Change code → PR (unit + render) → merge (dev deploy + integration + agent tests) → tag (prod).
Table DDL uses `CREATE TABLE IF NOT EXISTS`; schema changes must ship as additive
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in a new numbered file.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `NOT_AUTHORIZED: user X lacks SUGGEST on domain 'D'` | Grant the domain privilege to one of the user's roles via `SET_DOMAIN_PRIVILEGE`; for child domains also grant `USAGE` on each parent. |
| Concept "does not exist" for a user | By design when they lack `READ` (or parent `USAGE`). Check `SHOW GRANTS TO ROLE <role>`. |
| `NOT_FOUND ... not visible to the ontology service role` on a representation | Grant the service role `REFERENCES`/`SELECT` on the asset. |
| `separation of duties` | A different steward must approve. |
| `proposed edit is stale` | The approved version changed after the edit was proposed; re-propose. |
| Agent never asks to disambiguate | The caller can only see one meaning (correct), or check `V_AMBIGUOUS_TERMS` as that role. |
| Task suspended | `SUSPEND_TASK_AFTER_NUM_FAILURES = 3`; inspect `TASK_HISTORY`, fix, `ALTER TASK ... RESUME`. |

## Running tests from a single-identity environment

`scripts/actors.py` uses separate key-pair users (`users` mode) when it can log in as them. Where
only one identity can authenticate (e.g. a Snowsight sandbox that forces OAuth), it falls back to
`roles` mode: the human user holds exactly one persona role at a time with secondary roles off.
Visibility and privilege checks are fully exercised; because author and steward are the same user,
dev sets `REQUIRE_DISTINCT_APPROVER: FALSE` and the suite separately proves SoD enforcement by
switching it on. CI uses `users` mode.

## Migrating to native Business Ontology

When Snowflake Business Ontology becomes available in your account:

1. `CALL API.EXPORT_ONTOLOGY(NULL, 'YAML')` (or `'TURTLE'`) as an admin to export approved concepts,
   relationships and representations.
2. Import through the native supported file-import path (concepts re-enter steward review there).
3. Map domains 1:1; map `DOMAIN_ACCESS` roles to native domain privileges (USAGE/READ/SUGGEST/MODIFY
   are the same names).
4. Point the agent's ontology tool at the native `business_ontology` tool; keep the semantic-view tools.
5. Run both in parallel and compare answers with `tests/agent`; then set this deployment read-only
   (revoke `USAGE` on write procedures) and keep it as an audit archive.
