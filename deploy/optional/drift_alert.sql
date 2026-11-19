-- =============================================================================
-- Optional: notify stewards/admins when new drift findings appear.
-- Requires an email notification integration (ACCOUNTADMIN):
--   CREATE NOTIFICATION INTEGRATION ONTOLOGY_EMAIL TYPE = EMAIL ENABLED = TRUE
--     ALLOWED_RECIPIENTS = ('data-governance@example.com');
--   GRANT USAGE ON INTEGRATION ONTOLOGY_EMAIL TO ROLE ONTOLOGY_ADMIN;
-- Then render + run this file with your values substituted.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE SCHEMA {{DB}}.OPS;

CREATE OR REPLACE ALERT A_ONTOLOGY_DRIFT
  WAREHOUSE = {{WAREHOUSE}}
  SCHEDULE = '60 MINUTE'
  IF (EXISTS (
    SELECT 1 FROM {{DB}}.CORE.DRIFT_FINDING
     WHERE RESOLVED_AT IS NULL
       AND DETECTED_AT > SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME()))
  THEN
    CALL SYSTEM$SEND_EMAIL(
      'ONTOLOGY_EMAIL',
      'data-governance@example.com',
      'Business Ontology: new representation drift',
      'New drift findings need steward review. Query {{DB}}.API.V_DRIFT_OPEN.');

ALTER ALERT A_ONTOLOGY_DRIFT RESUME;
