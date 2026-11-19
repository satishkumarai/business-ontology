-- =============================================================================
-- 07  Consumer grants
--     ONTOLOGY_CONSUMER is granted to every domain USAGE role by CREATE_DOMAIN,
--     so anyone holding any domain privilege can reach the API. What they can
--     *see* and *do* is then decided per domain by policies and procedures.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};

GRANT USAGE ON DATABASE {{DB}}               TO ROLE {{CONSUMER_ROLE}};
GRANT USAGE ON SCHEMA {{DB}}.API             TO ROLE {{CONSUMER_ROLE}};
GRANT USAGE ON SCHEMA {{DB}}.AGENTS          TO ROLE {{CONSUMER_ROLE}};
GRANT SELECT ON ALL VIEWS IN SCHEMA {{DB}}.API       TO ROLE {{CONSUMER_ROLE}};
GRANT SELECT ON FUTURE VIEWS IN SCHEMA {{DB}}.API    TO ROLE {{CONSUMER_ROLE}};
GRANT USAGE ON ALL PROCEDURES IN SCHEMA {{DB}}.API   TO ROLE {{CONSUMER_ROLE}};
GRANT USAGE ON FUTURE PROCEDURES IN SCHEMA {{DB}}.API TO ROLE {{CONSUMER_ROLE}};
GRANT READ ON STAGE {{DB}}.API.IMPORT_STAGE  TO ROLE {{CONSUMER_ROLE}};

-- Explicitly no grants on CORE or OPS to consumers.
