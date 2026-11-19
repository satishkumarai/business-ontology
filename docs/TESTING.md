# Testing the Business Ontology in real time

Three ways to test, from quickest to most thorough. Replace `<your_user>` with your Snowflake user.

## 1. Agent chat in Snowsight

Open **AI & ML → Agents → ONTOLOGY_AGENT** (display name "Governed Analyst"). The chat shows each tool call as the agent works.

| # | Ask | Expected |
|---|---|---|
| 1 | `What was net revenue last quarter?` | Lists `Finance.Net Revenue` and `Sales.Net Revenue`, then asks which one you mean |
| 2 | `Finance` | Returns $3,767,662.13 for Q3 2026, with a Grounding block: definition, policy, exclusions, GOVERNED |
| 3 | `How do Finance and Sales define net revenue differently?` | Compares the two approved definitions and their time policies |
| 4 | `What is our churn rate?` | Says no approved concept exists, and labels any number EXPLORATORY |

## 2. Role-filtered behavior

What the agent knows depends on the role you chat with. ACCOUNTADMIN includes `ONTOLOGY_ADMIN`, so it sees every domain. Grant yourself a demo role and switch to it in the Snowsight role picker:

```sql
USE ROLE ACCOUNTADMIN;
GRANT ROLE ONT_DEMO_EXEC_ROLE        TO USER <your_user>;  -- Finance + Sales: asked to disambiguate
GRANT ROLE ONT_DEMO_FIN_ANALYST_ROLE TO USER <your_user>;  -- Finance only: answered directly
GRANT ROLE ONT_DEMO_OUTSIDER_ROLE    TO USER <your_user>;  -- no domains: concepts "do not exist"

-- If DEFAULT_SECONDARY_ROLES = ('ALL'), every granted role (including ACCOUNTADMIN) applies
-- in the session, and you will always get the admin view. Turn it off while testing:
ALTER USER <your_user> SET DEFAULT_SECONDARY_ROLES = ();
```

| Role | `SELECT DISTINCT DOMAIN_PATH FROM ONTOLOGY_DB.API.V_CONCEPT` | Agent on question 1 |
|---|---|---|
| ONT_DEMO_EXEC_ROLE | Finance, Sales | Asks which Net Revenue |
| ONT_DEMO_FIN_ANALYST_ROLE | Finance (not Finance.EMEA, because there is no inheritance) | Answers with Finance directly |
| ONT_DEMO_OUTSIDER_ROLE | (none) | No approved definition found |

## 3. Each piece directly in a worksheet

Run these as any of the roles above:

```sql
CALL ONTOLOGY_DB.API.SEARCH_CONCEPTS('net revenue');      -- exactly what the agent's tool returns
CALL ONTOLOGY_DB.API.GET_CONCEPT('Net Revenue');          -- unqualified: candidates, not a guess
CALL ONTOLOGY_DB.API.GET_CONCEPT('Finance.Net Revenue');  -- policies, exclusions, implementation, history
SELECT * FROM ONTOLOGY_DB.API.V_CONCEPT;                  -- changes with the active role
SELECT * FROM ONTOLOGY_DB.API.V_AMBIGUOUS_TERMS;          -- names with more than one meaning you can see
SELECT * FROM ONTOLOGY_DB.API.V_REVIEW_QUEUE;             -- drafts and edits awaiting review (author/steward)
CALL ONTOLOGY_DB.API.EXPORT_ONTOLOGY(NULL, 'YAML');       -- role-filtered export
```

Admin only (`USE ROLE ONTOLOGY_ADMIN`):

```sql
SELECT * FROM ONTOLOGY_DB.OPS.V_HEALTH;
SELECT * FROM ONTOLOGY_DB.OPS.V_STEWARD_WORKLOAD;
```

## 4. Watch governance happen live

The pending edit stays invisible to the agent until a steward approves it.

```sql
-- As the author (holds SUGGEST on Finance)
USE ROLE ACCOUNTADMIN; GRANT ROLE ONT_DEMO_AUTHOR_ROLE TO USER <your_user>;
USE ROLE ONT_DEMO_AUTHOR_ROLE;
CALL ONTOLOGY_DB.API.PROPOSE_EDIT('Finance.Net Revenue',
  OBJECT_CONSTRUCT('synonyms', ARRAY_CONSTRUCT('net sales', 'recognized revenue', 'revenue (net)')),
  'Add board-deck phrasing');
-- Copy the returned version_id.

-- As a reader: still version 1, with a pending edit flagged
USE ROLE ONT_DEMO_EXEC_ROLE;
SELECT VERSION_NO, SYNONYMS, HAS_PENDING_EDIT FROM ONTOLOGY_DB.API.V_CONCEPT
 WHERE QUALIFIED_NAME = 'Finance.Net Revenue';
-- Ask the agent about Finance net revenue: it cites v1.

-- As the Finance steward
USE ROLE ACCOUNTADMIN; GRANT ROLE ONT_DEMO_FIN_STEWARD_ROLE TO USER <your_user>;
USE ROLE ONT_DEMO_FIN_STEWARD_ROLE;
CALL ONTOLOGY_DB.API.APPROVE_VERSION('<version_id>', 'Reviewed');
-- Ask again: it cites v2. The audit trail shows both actions:
SELECT * FROM ONTOLOGY_DB.API.V_AUDIT WHERE DOMAIN_PATH = 'Finance' ORDER BY EVENT_TS DESC LIMIT 5;
```

Other experiments that are quick to try:

| Try | Expected |
|---|---|
| As FIN_ANALYST: `CALL ONTOLOGY_DB.API.PROPOSE_CONCEPT('Finance','X','TERM','Should be refused.')` | `NOT_AUTHORIZED` |
| As AUTHOR: propose a `TERM` with a formula | `VALIDATION_ERROR: only ('METRIC',) concepts may carry formula…` |
| As AUTHOR: `PROPOSE_RELATIONSHIP('Finance.Invoice','EQUIVALENT_TO','Finance.Customer')` | Rejected: same-domain duplicates must be merged |
| As FIN_STEWARD: `SELECT * FROM ONTOLOGY_DB.CORE.CONCEPT` | Error: no direct access to the system of record |

### Drift (admin + steward)

```sql
USE ROLE ONTOLOGY_ADMIN;
-- Change the governed metric's definition in the semantic view (e.g. re-create FINANCE_REVENUE_SV with
-- a different NET_REVENUE expression or comment), then:
CALL ONTOLOGY_DB.CORE.VALIDATE_REPRESENTATIONS();
USE ROLE ONT_DEMO_FIN_STEWARD_ROLE;
SELECT * FROM ONTOLOGY_DB.API.V_DRIFT_OPEN;               -- FORMULA_CHANGED / DESCRIPTION_CHANGED
CALL ONTOLOGY_DB.API.ACKNOWLEDGE_DRIFT('<finding_id>', 'ACCEPT', 'Change reviewed');
-- Restore the original view afterwards: python scripts/deploy.py --env dev --only 10
```

## 5. Automated suites

```bash
bash deploy.sh --test             # everything
python -m pytest tests/unit       # 21 offline rule tests
python -m pytest tests/integration -v   # 21 governance tests as six personas (~15 min)
python -m pytest tests/agent -v         # agent clarification and number accuracy
```

## Cleanup

```sql
USE ROLE ACCOUNTADMIN;
REVOKE ROLE ONT_DEMO_EXEC_ROLE        FROM USER <your_user>;
REVOKE ROLE ONT_DEMO_FIN_ANALYST_ROLE FROM USER <your_user>;
REVOKE ROLE ONT_DEMO_OUTSIDER_ROLE    FROM USER <your_user>;
REVOKE ROLE ONT_DEMO_AUTHOR_ROLE      FROM USER <your_user>;
REVOKE ROLE ONT_DEMO_FIN_STEWARD_ROLE FROM USER <your_user>;
ALTER USER <your_user> SET DEFAULT_SECONDARY_ROLES = ('ALL');   -- restore if you changed it
```
