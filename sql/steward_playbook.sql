-- =============================================================================
-- Steward / author playbook: copy-paste SQL for day-to-day work.
-- Run with a role that holds the relevant domain privilege.
-- =============================================================================

-- ---------------------------------------------------------------- browse
SELECT * FROM ONTOLOGY_DB.API.V_DOMAIN;
SELECT QUALIFIED_NAME, KIND, DEFINITION, SYNONYMS, VERSION_NO, APPROVED_BY
  FROM ONTOLOGY_DB.API.V_CONCEPT ORDER BY QUALIFIED_NAME;
SELECT * FROM ONTOLOGY_DB.API.V_AMBIGUOUS_TERMS;          -- names with >1 approved meaning
CALL ONTOLOGY_DB.API.SEARCH_CONCEPTS('net revenue');
CALL ONTOLOGY_DB.API.GET_CONCEPT('Finance.Net Revenue');

-- ------------------------------------------------------------ author (SUGGEST)
CALL ONTOLOGY_DB.API.PROPOSE_CONCEPT(
  'Finance', 'Annual Recurring Revenue', 'METRIC',
  'Annualized value of active subscription contracts at period end.',
  ARRAY_CONSTRUCT('ARR'), 'SUM(mrr) * 12', ARRAY_CONSTRUCT('one-time fees'),
  'Finance metrics handbook 2026, section 2');

CALL ONTOLOGY_DB.API.PROPOSE_EDIT('Finance.Net Revenue',
  OBJECT_CONSTRUCT('synonyms', ARRAY_CONSTRUCT('net sales', 'recognized revenue', 'revenue (net)')),
  'Add common board-deck phrasing');

CALL ONTOLOGY_DB.API.PROPOSE_RELATIONSHIP('Finance.Gross Revenue', 'DERIVES', 'Finance.Annual Recurring Revenue');
CALL ONTOLOGY_DB.API.PROPOSE_RELATIONSHIP('Finance.Customer', 'EQUIVALENT_TO', 'Sales.Customer');   -- cross-domain only
CALL ONTOLOGY_DB.API.PROPOSE_REPRESENTATION('Finance.Net Revenue', 'RELATED_DASHBOARD', 'BI_ASSET',
  'https://bi.example.com/dashboards/finance-close');

-- Bootstrap drafts from an existing semantic view, or import a glossary file
CALL ONTOLOGY_DB.API.BOOTSTRAP_FROM_SEMANTIC_VIEW('ANALYTICS.REPORTING.REVENUE_SV', 'Finance');
-- PUT file:///path/glossary.yaml @ONTOLOGY_DB.API.IMPORT_STAGE/team/ AUTO_COMPRESS=FALSE;
CALL ONTOLOGY_DB.API.IMPORT_GLOSSARY('@ONTOLOGY_DB.API.IMPORT_STAGE/team/glossary.yaml', 'AUTO', NULL, TRUE);  -- dry run first

-- ------------------------------------------------------------ steward (MODIFY)
SELECT ITEM_TYPE, ITEM_ID, SUBJECT, CHANGE, AUTHOR, CREATED_AT, DETAILS
  FROM ONTOLOGY_DB.API.V_REVIEW_QUEUE ORDER BY CREATED_AT;

CALL ONTOLOGY_DB.API.APPROVE_VERSION('<version_id>', 'Matches close manual');
CALL ONTOLOGY_DB.API.REJECT_VERSION('<version_id>', 'Duplicate of Finance.Net Revenue; propose a synonym instead');
CALL ONTOLOGY_DB.API.REVIEW_LINK('RELATIONSHIP', '<rel_id>', 'APPROVE');
CALL ONTOLOGY_DB.API.REVIEW_LINK('REPRESENTATION', '<rep_id>', 'REJECT', 'Points at a deprecated view');
CALL ONTOLOGY_DB.API.MERGE_CONCEPTS('Finance.Net Revenue', 'Finance.Net Sales', 'Same concept');
CALL ONTOLOGY_DB.API.RETIRE_CONCEPT('Finance.Legacy Bookings', 'Replaced by Sales.Gross Bookings');

-- Drift
SELECT * FROM ONTOLOGY_DB.API.V_DRIFT_OPEN;
CALL ONTOLOGY_DB.API.ACKNOWLEDGE_DRIFT('<finding_id>', 'ACCEPT', 'Formula change reviewed with Finance');
CALL ONTOLOGY_DB.API.ACKNOWLEDGE_DRIFT('<finding_id>', 'RETIRE_LINK', 'Metric removed from semantic view');

-- Audit
SELECT * FROM ONTOLOGY_DB.API.V_AUDIT WHERE DOMAIN_PATH = 'Finance' ORDER BY EVENT_TS DESC LIMIT 100;

-- ------------------------------------------------------------ owner / admin
CALL ONTOLOGY_DB.API.CREATE_DOMAIN('Finance.Revenue', 'Revenue sub-domain', 'FINANCE_OWNER', 'FINANCE_STEWARD');
CALL ONTOLOGY_DB.API.SET_DOMAIN_PRIVILEGE('Finance', 'READ', 'ANALYST', 'GRANT');
CALL ONTOLOGY_DB.API.SET_DOMAIN_PRIVILEGE('Finance', 'SUGGEST', 'FINANCE_ANALYTICS_ENG', 'GRANT');
SELECT * FROM ONTOLOGY_DB.OPS.V_HEALTH;
SELECT * FROM ONTOLOGY_DB.OPS.V_STEWARD_WORKLOAD;

-- Export (role-filtered)
CALL ONTOLOGY_DB.API.EXPORT_ONTOLOGY('Finance', 'YAML');
CALL ONTOLOGY_DB.API.EXPORT_ONTOLOGY(NULL, 'TURTLE');
