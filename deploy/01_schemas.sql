-- =============================================================================
-- 01  Schemas, stages, settings
--     CORE      system of record (no direct consumer access)
--     API       secure views + procedures (the only consumer surface)
--     OPS       tasks, health views
--     DEMO      demo source tables      (omit in prod via --skip-sample)
--     SEMANTIC  demo semantic views     (omit in prod via --skip-sample)
--     AGENTS    Cortex Agent(s)
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE DATABASE {{DB}};
USE WAREHOUSE {{WAREHOUSE}};

CREATE SCHEMA IF NOT EXISTS CORE   WITH MANAGED ACCESS COMMENT = 'Ontology system of record';
CREATE SCHEMA IF NOT EXISTS API    WITH MANAGED ACCESS COMMENT = 'Consumer surface: secure views and procedures';
CREATE SCHEMA IF NOT EXISTS OPS    WITH MANAGED ACCESS COMMENT = 'Operations: tasks and health';
CREATE SCHEMA IF NOT EXISTS AGENTS WITH MANAGED ACCESS COMMENT = 'Cortex Agents grounded on the ontology';

CREATE STAGE IF NOT EXISTS CORE.CODE_STAGE
  DIRECTORY = (ENABLE = FALSE) ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  COMMENT = 'Python package for ontology procedures';
CREATE STAGE IF NOT EXISTS API.IMPORT_STAGE
  DIRECTORY = (ENABLE = TRUE) ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  COMMENT = 'Drop glossary files (YAML/JSON/TTL) here, then CALL API.IMPORT_GLOSSARY';

CREATE TABLE IF NOT EXISTS CORE.SETTINGS (
  KEY        STRING NOT NULL PRIMARY KEY,
  VALUE      VARIANT,
  UPDATED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

MERGE INTO CORE.SETTINGS S
USING (
  SELECT 'ADMIN_ROLE' K, TO_VARIANT('{{ADMIN_ROLE}}') V UNION ALL
  SELECT 'CONSUMER_ROLE', TO_VARIANT('{{CONSUMER_ROLE}}') UNION ALL
  SELECT 'ROLE_PREFIX', TO_VARIANT('{{ROLE_PREFIX}}') UNION ALL
  SELECT 'REQUIRE_DISTINCT_APPROVER', TO_VARIANT({{REQUIRE_DISTINCT_APPROVER}}) UNION ALL
  SELECT 'ENVIRONMENT', TO_VARIANT('{{ENV}}')
) N ON S.KEY = N.K
WHEN MATCHED THEN UPDATE SET VALUE = N.V, UPDATED_AT = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (N.K, N.V);
