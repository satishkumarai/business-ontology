-- =============================================================================
-- 03  Row access policies: domain-scoped visibility
--
--  * Every strict ancestor of the object's domain must be navigable
--    (caller has that ancestor's USAGE role); no inheritance to children.
--  * READ    sees APPROVED (and SUPERSEDED history) content.
--  * SUGGEST additionally sees the drafts they authored.
--  * MODIFY  (steward) sees everything in the domain, including drafts.
--  * For anyone else the row does not exist.
--  Privilege roles are nested MODIFY > SUGGEST > READ > USAGE, so
--  IS_ROLE_IN_SESSION on the lower role is true for holders of higher ones.
--  Policies evaluate against the querying session, so they also govern
--  CALLER's-rights procedures (search, export) and Cortex Agent tools.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE SCHEMA {{DB}}.CORE;

CREATE ROW ACCESS POLICY IF NOT EXISTS RAP_ONTOLOGY_OBJECT
  AS (P_DOMAIN_ID STRING, P_STATUS STRING, P_AUTHOR STRING) RETURNS BOOLEAN -> TRUE;

ALTER ROW ACCESS POLICY RAP_ONTOLOGY_OBJECT SET BODY ->
  IS_ROLE_IN_SESSION('{{ADMIN_ROLE}}')
  OR (
    (SELECT COUNT(*) FROM {{DB}}.CORE.DOMAIN_ANCESTOR A
      WHERE A.DOMAIN_ID = P_DOMAIN_ID AND A.DEPTH > 0)
    =
    (SELECT COUNT(DISTINCT A.ANCESTOR_ID)
       FROM {{DB}}.CORE.DOMAIN_ANCESTOR A
       JOIN {{DB}}.CORE.DOMAIN_ACCESS X
         ON X.DOMAIN_ID = A.ANCESTOR_ID AND X.PRIVILEGE = 'USAGE'
      WHERE A.DOMAIN_ID = P_DOMAIN_ID AND A.DEPTH > 0
        AND IS_ROLE_IN_SESSION(X.ROLE_NAME))
    AND EXISTS (
      SELECT 1 FROM {{DB}}.CORE.DOMAIN_ACCESS X
       WHERE X.DOMAIN_ID = P_DOMAIN_ID
         AND IS_ROLE_IN_SESSION(X.ROLE_NAME)
         AND (   X.PRIVILEGE = 'MODIFY'
              OR (X.PRIVILEGE = 'READ'    AND P_STATUS IN ('APPROVED', 'SUPERSEDED'))
              OR (X.PRIVILEGE = 'SUGGEST' AND P_AUTHOR = CURRENT_USER())))
  );

CREATE ROW ACCESS POLICY IF NOT EXISTS RAP_DOMAIN
  AS (P_DOMAIN_ID STRING) RETURNS BOOLEAN -> TRUE;

ALTER ROW ACCESS POLICY RAP_DOMAIN SET BODY ->
  IS_ROLE_IN_SESSION('{{ADMIN_ROLE}}')
  OR (SELECT COUNT(*) FROM {{DB}}.CORE.DOMAIN_ANCESTOR A WHERE A.DOMAIN_ID = P_DOMAIN_ID)
     =
     (SELECT COUNT(DISTINCT A.ANCESTOR_ID)
        FROM {{DB}}.CORE.DOMAIN_ANCESTOR A
        JOIN {{DB}}.CORE.DOMAIN_ACCESS X
          ON X.DOMAIN_ID = A.ANCESTOR_ID AND X.PRIVILEGE = 'USAGE'
       WHERE A.DOMAIN_ID = P_DOMAIN_ID
         AND IS_ROLE_IN_SESSION(X.ROLE_NAME));

-- Attach once (idempotent).
EXECUTE IMMEDIATE $$
DECLARE
  targets ARRAY := ARRAY_CONSTRUCT(
    ARRAY_CONSTRUCT('CONCEPT',         'RAP_ONTOLOGY_OBJECT', 'DOMAIN_ID, STATUS, CREATED_BY'),
    ARRAY_CONSTRUCT('CONCEPT_VERSION', 'RAP_ONTOLOGY_OBJECT', 'DOMAIN_ID, STATUS, AUTHOR'),
    ARRAY_CONSTRUCT('RELATIONSHIP',    'RAP_ONTOLOGY_OBJECT', 'DOMAIN_ID, STATUS, CREATED_BY'),
    ARRAY_CONSTRUCT('REPRESENTATION',  'RAP_ONTOLOGY_OBJECT', 'DOMAIN_ID, STATUS, CREATED_BY'),
    -- Audit + drift: STATUS-position column never equals APPROVED, so only
    -- stewards (MODIFY) see them; authors see their own audit events.
    ARRAY_CONSTRUCT('AUDIT_EVENT',     'RAP_ONTOLOGY_OBJECT', 'DOMAIN_ID, ACTION, ACTOR_USER'),
    ARRAY_CONSTRUCT('DRIFT_FINDING',   'RAP_ONTOLOGY_OBJECT', 'DOMAIN_ID, FINDING_TYPE, RESOLVED_BY'),
    ARRAY_CONSTRUCT('DOMAIN',          'RAP_DOMAIN',          'DOMAIN_ID'));
  attached INTEGER DEFAULT 0;
BEGIN
  FOR i IN 0 TO ARRAY_SIZE(:targets) - 1 DO
    LET tbl STRING := targets[i][0];
    LET pol STRING := targets[i][1];
    LET cols STRING := targets[i][2];
    BEGIN
      EXECUTE IMMEDIATE 'ALTER TABLE {{DB}}.CORE.' || tbl || ' ADD ROW ACCESS POLICY {{DB}}.CORE.' || pol || ' ON (' || cols || ')';
      attached := attached + 1;
    EXCEPTION
      WHEN OTHER THEN NULL;  -- already attached
    END;
  END FOR;
  RETURN 'policies newly attached: ' || attached;
END;
$$;
