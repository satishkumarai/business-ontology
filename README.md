# Governed Business Ontology on Snowflake

A deployable implementation of a **governed business ontology for AI agents**: steward-approved,
domain-qualified business meaning that lives next to the data, is filtered by role, and is wired to
the semantic views that compute the numbers.

It follows the model Snowflake describes for *Business Ontology* (private preview at the time of
writing), built entirely from GA primitives (tables, row access policies, owner's-rights
procedures, semantic views, Cortex Agents) so it runs today and can be migrated to the native
feature later (see [docs/RUNBOOK.md](docs/RUNBOOK.md#migrating-to-native-business-ontology)).

```
          meaning (who approved it?)            computation (what's the number?)
  ┌──────────────────────────────────┐     ┌──────────────────────────────────┐
  │ ONTOLOGY_DB.CORE   system of record│     │ semantic views (yours)            │
  │  domains · concepts · versions     │──── │  FINANCE_REVENUE_SV               │
  │  relationships · representations   │ rep │  SALES_BOOKINGS_SV                │
  │  drift · audit · import batches    │     └──────────────────────────────────┘
  └───────────────▲───────────────────┘                    ▲
     owner's-rights│governed writes        row access     │ cortex analyst
  ┌───────────────┴───────────────────┐  policies   ┌──────┴────────────────────┐
  │ ONTOLOGY_DB.API  procedures + views│◀───────────│ AGENTS.ONTOLOGY_AGENT      │
  │  PROPOSE_* · APPROVE · MERGE · …   │  caller's   │  search_business_ontology  │
  │  SEARCH_CONCEPTS · GET_CONCEPT     │  rights     │  get_concept_details       │
  └────────────────────────────────────┘             └───────────────────────────┘
```

## What you get

| Capability | Where |
|---|---|
| Nested domains (`Finance.EMEA`) with generated `USAGE/READ/SUGGEST/MODIFY` roles, no inheritance to children, parent-path navigation required | `API.CREATE_DOMAIN`, `CORE.RAP_*` |
| Concepts with kind, scope, synonyms; **formula/exclusions/provenance on METRIC only**; append-only versions | `CORE.CONCEPT`, `CORE.CONCEPT_VERSION` |
| Draft → approve; edits to approved concepts stay *pending* while agents keep using the approved version; stale-edit detection; reject requires a note; optional separation of duties | `API.PROPOSE_EDIT`, `API.APPROVE_VERSION` |
| Fixed typed edges (`IS_A, HAS_PART, DERIVES, MEASURES, APPLIES_TO, EQUIVALENT_TO, RELATED_TO, CUSTOM+label`) with kind rules and cycle checks | `API.PROPOSE_RELATIONSHIP` |
| Representations to tables/views/columns/semantic-view metrics/BI assets, **validated on creation**, snapshotted for drift | `API.PROPOSE_REPRESENTATION` |
| Scheduled drift detection (missing asset, formula/description/type/schema change), steward acknowledgement | `OPS.T_VALIDATE_REPRESENTATIONS`, `API.V_DRIFT_OPEN` |
| Same-domain merge (cross-domain uses `EQUIVALENT_TO`), retire | `API.MERGE_CONCEPTS`, `API.RETIRE_CONCEPT` |
| Ingestion: YAML/JSON glossary, SKOS/RDF Turtle, semantic-view bootstrap, Cortex Sense promotion — **all land as DRAFT** | `API.IMPORT_GLOSSARY`, `API.BOOTSTRAP_FROM_SEMANTIC_VIEW`, `API.PROMOTE_CORTEX_SENSE_FINDING` |
| Role-filtered search with ambiguity flags; role-filtered export (JSON/YAML/Turtle) | `API.SEARCH_CONCEPTS`, `API.EXPORT_ONTOLOGY` |
| Cortex Agent that disambiguates, cites the approved definition + policy, labels numbers GOVERNED / DERIVED / EXPLORATORY | `AGENTS.ONTOLOGY_AGENT` |
| Full audit trail; health + steward workload views | `API.V_AUDIT`, `OPS.V_HEALTH` |

## Quick start

**One shot:** `bash deploy.sh` (dev: unit tests → deploy → seed → smoke test).
Options: `--env prod`, `--connection <name>`, `--skip-bootstrap`, `--test` (adds integration + agent tests), `--no-seed`.

Step by step:

```bash
pip install -r requirements.txt
make unit                              # 21 offline tests
python scripts/deploy.py --env dev     # idempotent; uses connections.toml, OAuth token or key-pair env vars
python scripts/seed_demo.py --env dev  # Finance / Sales / Finance.EMEA through the governed path
make test                              # 21 integration tests as different personas
make test-agent                        # agent behavioural evaluation
```

Then, as a role with Finance **and** Sales READ, ask the agent *"What was net revenue last quarter?"*.
It answers with the two approved meanings, asks which one you mean, and then returns the governed
Finance number with its grounding:

```
Grounding
- Definition: Finance.Net Revenue v1, approved by … — Recognized revenue after refunds and discounts…
- Policies applied: Finance.Revenue Recognition Policy (periods by recognition date)
- Exclusions: intercompany invoices, test accounts
- Number: GOVERNED (semantic-view metric FINANCE_REVENUE_SV.INVOICES.NET_REVENUE)
```

## Repository layout

```
config/environments.yml        per-environment DB, roles, role prefix, SoD, schedule
deploy/00..11_*.sql            ordered, idempotent, templated ({{DB}}, {{ADMIN_ROLE}}, …) deployment
deploy/optional/               drift e-mail alert
src/python/ontology/           procedure package (validation, authz, api, drift, importer, exporter)
scripts/deploy.py              renderer + runner + package upload (dry-run supported)
scripts/seed_demo.py           demo domains/personas/glossaries through the governed API
scripts/actors.py, personas.py run as distinct personas (key-pair users, or single-identity role mode)
scripts/teardown.py            data wipe / full teardown (refuses prod)
seed/glossary/                 finance.yaml, sales.yaml, finance_emea.ttl (SKOS)
sql/steward_playbook.sql       day-to-day SQL for authors, stewards, owners
tests/unit | integration | agent
docs/ARCHITECTURE.md           design decisions, security model, data model
docs/RUNBOOK.md                production rollout, operations, troubleshooting, migration
.github/workflows/ci.yml       PR → unit; main → dev deploy + tests; tag → prod
```

## Production rollout (summary)

1. Set `config/environments.yml → prod` (DB, roles, prefix). Keep `REQUIRE_DISTINCT_APPROVER: TRUE`.
2. Platform admin runs step `00` once (needs ACCOUNTADMIN); CI then deploys with `--skip-bootstrap`.
3. Create domains with real owner/steward roles; grant READ/SUGGEST to business roles.
4. Import existing glossaries or bootstrap from your semantic views; stewards approve.
5. Point `deploy/11_agent.sql` analyst tools at your semantic views, set `include_agent: true`.

Details in [docs/RUNBOOK.md](docs/RUNBOOK.md). Hands-on test scenarios (agent chat, role switching,
live approval, drift) are in [docs/TESTING.md](docs/TESTING.md).
