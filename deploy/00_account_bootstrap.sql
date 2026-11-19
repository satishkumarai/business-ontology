-- =============================================================================
-- 00  Account bootstrap (run as ACCOUNTADMIN or a role with MANAGE GRANTS)
--     Idempotent. Creates the service role that owns every ontology object.
-- =============================================================================
USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS {{ADMIN_ROLE}}
  COMMENT = 'Business Ontology service/admin role: owns ontology objects. Not an approver.';
CREATE ROLE IF NOT EXISTS {{CONSUMER_ROLE}}
  COMMENT = 'Business Ontology consumer: read API views and call governed procedures.';

GRANT ROLE {{ADMIN_ROLE}} TO ROLE SYSADMIN;
GRANT OWNERSHIP ON ROLE {{CONSUMER_ROLE}} TO ROLE {{ADMIN_ROLE}} COPY CURRENT GRANTS;

-- Domain privilege roles are created dynamically by API.CREATE_DOMAIN.
GRANT CREATE ROLE ON ACCOUNT TO ROLE {{ADMIN_ROLE}};
GRANT CREATE DATABASE ON ACCOUNT TO ROLE {{ADMIN_ROLE}};
GRANT EXECUTE TASK ON ACCOUNT TO ROLE {{ADMIN_ROLE}};
-- Lets the governed procedures resolve the caller's role hierarchy
-- (SHOW GRANTS OF ROLE) for business roles the service role does not own.
GRANT MANAGE GRANTS ON ACCOUNT TO ROLE {{ADMIN_ROLE}};
GRANT USAGE ON WAREHOUSE {{WAREHOUSE}} TO ROLE {{ADMIN_ROLE}};

-- Cortex functions (agent + analyst tools).
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE {{ADMIN_ROLE}};
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE {{CONSUMER_ROLE}};

CREATE DATABASE IF NOT EXISTS {{DB}} COMMENT = 'Governed Business Ontology';
GRANT OWNERSHIP ON DATABASE {{DB}} TO ROLE {{ADMIN_ROLE}} COPY CURRENT GRANTS;
