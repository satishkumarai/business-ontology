-- =============================================================================
-- 05  Governed write API (Python, OWNER's rights)
--     Callers never get DML on CORE. Each procedure authorizes the real caller
--     (CURRENT_USER) against domain privilege roles, validates, writes and
--     audits in one transaction. See src/python/ontology/api.py.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE SCHEMA {{DB}}.API;

-- ---------------------------------------------------------------- domains
CREATE OR REPLACE PROCEDURE CREATE_DOMAIN(
    PATH STRING, DESCRIPTION STRING DEFAULT NULL, OWNER_ROLE STRING DEFAULT NULL, STEWARD_ROLE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.create_domain' COMMENT = 'Admin: create a (nested) domain and its USAGE/READ/SUGGEST/MODIFY roles'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE SET_DOMAIN_PRIVILEGE(
    PATH STRING, PRIVILEGE STRING, GRANTEE_ROLE STRING, ACTION STRING DEFAULT 'GRANT')
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.set_domain_privilege' COMMENT = 'Domain owner/admin: GRANT or REVOKE a domain privilege to/from a business role'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE DROP_DOMAIN(PATH STRING)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.drop_domain' COMMENT = 'Domain owner/admin: drop an empty domain'
  EXECUTE AS OWNER;

-- --------------------------------------------------------------- concepts
CREATE OR REPLACE PROCEDURE PROPOSE_CONCEPT(
    DOMAIN_PATH STRING, NAME STRING, KIND STRING, DEFINITION STRING,
    SYNONYMS ARRAY DEFAULT NULL, FORMULA STRING DEFAULT NULL, EXCLUSIONS ARRAY DEFAULT NULL,
    FORMULA_PROVENANCE STRING DEFAULT NULL, SCOPE STRING DEFAULT NULL,
    SOURCE STRING DEFAULT 'MANUAL', SOURCE_REF STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.propose_concept' COMMENT = 'SUGGEST: draft a new concept'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE PROPOSE_EDIT(QUALIFIED_NAME STRING, CHANGES OBJECT, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.propose_edit' COMMENT = 'SUGGEST: propose an edit; the approved version stays authoritative until approval'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE APPROVE_VERSION(VERSION_ID STRING, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.approve_version' COMMENT = 'MODIFY (steward): approve a draft or proposed edit'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE REJECT_VERSION(VERSION_ID STRING, NOTE STRING)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.reject_version' COMMENT = 'MODIFY (steward): reject a draft or proposed edit with a note'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE MERGE_CONCEPTS(SURVIVOR STRING, DUPLICATE STRING, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.merge_concepts' COMMENT = 'MODIFY (steward): merge a same-domain duplicate into a survivor'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE RETIRE_CONCEPT(QUALIFIED_NAME STRING, NOTE STRING)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.retire_concept' COMMENT = 'MODIFY (steward): retire a concept and its links'
  EXECUTE AS OWNER;

-- ------------------------------------------------------------------ links
CREATE OR REPLACE PROCEDURE PROPOSE_RELATIONSHIP(
    SOURCE STRING, REL_TYPE STRING, TARGET STRING, CUSTOM_LABEL STRING DEFAULT NULL, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.propose_relationship' COMMENT = 'SUGGEST (source domain): propose a typed edge'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE PROPOSE_REPRESENTATION(
    QUALIFIED_NAME STRING, REP_TYPE STRING, ASSET_KIND STRING, ASSET_FQN STRING,
    ASSET_MEMBER STRING DEFAULT NULL, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.propose_representation' COMMENT = 'SUGGEST: link a concept to a validated asset (snapshot taken as drift baseline)'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE REVIEW_LINK(LINK_KIND STRING, LINK_ID STRING, DECISION STRING, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.review_link' COMMENT = 'MODIFY: APPROVE/REJECT a RELATIONSHIP or REPRESENTATION'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE ACKNOWLEDGE_DRIFT(FINDING_ID STRING, RESOLUTION STRING, NOTE STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.acknowledge_drift' COMMENT = 'MODIFY: ACCEPT (re-baseline) or RETIRE_LINK for a drift finding'
  EXECUTE AS OWNER;

-- -------------------------------------------------------------- ingestion
CREATE OR REPLACE PROCEDURE PROMOTE_CORTEX_SENSE_FINDING(DOMAIN_PATH STRING, FINDING VARIANT)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.promote_cortex_sense_finding' COMMENT = 'SUGGEST: promote an inferred finding {name, kind, definition, synonyms, formula, evidence} to DRAFT'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE BOOTSTRAP_FROM_SEMANTIC_VIEW(
    SEMANTIC_VIEW STRING, DOMAIN_PATH STRING, INCLUDE_DIMENSIONS BOOLEAN DEFAULT FALSE)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.bootstrap_from_semantic_view' COMMENT = 'SUGGEST: propose DRAFT concepts + IMPLEMENTS links from semantic-view metrics'
  EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE IMPORT_GLOSSARY(
    STAGE_PATH STRING, FORMAT STRING DEFAULT 'AUTO', DEFAULT_DOMAIN STRING DEFAULT NULL, DRY_RUN BOOLEAN DEFAULT FALSE)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml', 'rdflib') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.import_glossary' COMMENT = 'SUGGEST: import YAML/JSON/SKOS glossary from @API.IMPORT_STAGE as DRAFTs'
  EXECUTE AS OWNER;

-- ------------------------------------------------------- export (caller)
CREATE OR REPLACE PROCEDURE EXPORT_ONTOLOGY(DOMAIN_PATH STRING DEFAULT NULL, FORMAT STRING DEFAULT 'JSON')
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.exporter.export_ontology' COMMENT = 'Export caller-visible approved concepts as JSON, YAML or SKOS Turtle'
  EXECUTE AS CALLER;

-- -------------------------------------------- scheduled (admin/task only)
CREATE OR REPLACE PROCEDURE {{DB}}.CORE.VALIDATE_REPRESENTATIONS(DOMAIN_PATH STRING DEFAULT NULL)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '{{PY_RUNTIME}}'
  PACKAGES = ('snowflake-snowpark-python', 'pyyaml') IMPORTS = ('@{{DB}}.CORE.CODE_STAGE/ontology.zip')
  HANDLER = 'ontology.api.validate_representations' COMMENT = 'Re-snapshot every live representation and record drift findings'
  EXECUTE AS OWNER;
