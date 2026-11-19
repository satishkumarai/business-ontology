-- =============================================================================
-- 06  Retrieval API for people and agents (SQL, CALLER's rights)
--     Caller's rights => row access policies apply, so results are filtered by
--     the caller's role and domain-path access. These back the Cortex Agent's
--     business_ontology tools.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE SCHEMA {{DB}}.API;

CREATE OR REPLACE PROCEDURE SEARCH_CONCEPTS(
    QUERY STRING, DOMAIN_FILTER STRING DEFAULT NULL, KIND_FILTER STRING DEFAULT NULL, MAX_RESULTS INTEGER DEFAULT 8)
  RETURNS VARIANT
  LANGUAGE SQL
  COMMENT = 'Search approved, caller-visible concepts by name, synonym and definition. Flags ambiguous terms.'
  EXECUTE AS CALLER
AS
$$
DECLARE
  q   STRING  DEFAULT LOWER(TRIM(COALESCE(:QUERY, '')));
  df  STRING  DEFAULT NULLIF(TRIM(COALESCE(:DOMAIN_FILTER, '')), '');
  kf  STRING  DEFAULT NULLIF(UPPER(TRIM(COALESCE(:KIND_FILTER, ''))), '');
  lim INTEGER DEFAULT LEAST(GREATEST(COALESCE(:MAX_RESULTS, 8), 1), 25);
  res VARIANT;
BEGIN
  IF (LENGTH(q) < 2) THEN
    RETURN OBJECT_CONSTRUCT('error', 'query must be at least 2 characters');
  END IF;

  res := (
    WITH c AS (
      SELECT * FROM {{DB}}.API.V_CONCEPT
       WHERE (:df IS NULL OR UPPER(DOMAIN_PATH) = UPPER(:df) OR STARTSWITH(UPPER(DOMAIN_PATH), UPPER(:df) || '.'))
         AND (:kf IS NULL OR KIND = :kf)
    ),
    syn AS (
      SELECT c.CONCEPT_ID, LOWER(s.VALUE::STRING) AS SYN
        FROM c, LATERAL FLATTEN(INPUT => c.SYNONYMS) s
    ),
    syn_score AS (
      SELECT CONCEPT_ID,
             MAX(CASE WHEN SYN = :q THEN 92
                      WHEN CONTAINS(:q, SYN) THEN 75
                      WHEN CONTAINS(SYN, :q) THEN 55 ELSE 0 END) AS S
        FROM syn GROUP BY CONCEPT_ID
    ),
    scored AS (
      SELECT c.*,
             GREATEST(
               IFF(LOWER(c.NAME) = :q OR LOWER(c.QUALIFIED_NAME) = :q, 100, 0),
               IFF(CONTAINS(:q, LOWER(c.NAME)), 85, 0),
               COALESCE(ss.S, 0),
               IFF(CONTAINS(LOWER(c.NAME), :q), 65, 0),
               IFF(CONTAINS(LOWER(c.DEFINITION), :q), 35, 0),
               IFF(JAROWINKLER_SIMILARITY(LOWER(c.NAME), :q) >= 90, JAROWINKLER_SIMILARITY(LOWER(c.NAME), :q) - 30, 0)
             ) AS SCORE
        FROM c LEFT JOIN syn_score ss ON ss.CONCEPT_ID = c.CONCEPT_ID
    ),
    top_hits AS (
      SELECT *, COUNT(*) OVER (PARTITION BY LOWER(NAME)) AS SAME_NAME
        FROM scored WHERE SCORE >= 50
      QUALIFY ROW_NUMBER() OVER (ORDER BY SCORE DESC, QUALIFIED_NAME) <= :lim
    ),
    reps AS (
      SELECT QUALIFIED_NAME,
             ARRAY_AGG(OBJECT_CONSTRUCT('type', REP_TYPE, 'asset_kind', ASSET_KIND, 'asset', ASSET_FQN,
                                        'member', ASSET_MEMBER, 'validation_status', VALIDATION_STATUS)) AS R
        FROM {{DB}}.API.V_REPRESENTATION GROUP BY QUALIFIED_NAME
    ),
    pols AS (
      SELECT r.TARGET_QNAME,
             ARRAY_AGG(OBJECT_CONSTRUCT('policy', r.SOURCE_QNAME, 'rule', p.DEFINITION)) AS P
        FROM {{DB}}.API.V_RELATIONSHIP r JOIN {{DB}}.API.V_CONCEPT p ON p.QUALIFIED_NAME = r.SOURCE_QNAME
       WHERE r.REL_TYPE = 'APPLIES_TO' GROUP BY r.TARGET_QNAME
    ),
    edges AS (
      SELECT QN, ARRAY_AGG(E) AS E FROM (
        SELECT SOURCE_QNAME AS QN, OBJECT_CONSTRUCT('direction', 'out', 'type', COALESCE(CUSTOM_LABEL, REL_TYPE), 'concept', TARGET_QNAME) AS E
          FROM {{DB}}.API.V_RELATIONSHIP WHERE REL_TYPE <> 'APPLIES_TO'
        UNION ALL
        SELECT TARGET_QNAME, OBJECT_CONSTRUCT('direction', 'in', 'type', COALESCE(CUSTOM_LABEL, REL_TYPE), 'concept', SOURCE_QNAME)
          FROM {{DB}}.API.V_RELATIONSHIP WHERE REL_TYPE <> 'APPLIES_TO'
      ) GROUP BY QN
    )
    SELECT OBJECT_CONSTRUCT(
             'query', :QUERY,
             'result_count', COUNT(*),
             'ambiguous_terms', ARRAY_UNIQUE_AGG(IFF(t.SAME_NAME > 1, LOWER(t.NAME), NULL)),
             'guidance', IFF(MAX(t.SAME_NAME) > 1,
                 'Several approved concepts share a name. Unless the user context clearly selects one, list the domain-qualified options and ask which one they mean; always state which one you used.',
                 'Use the domain-qualified name when citing a concept.'),
             'results', ARRAY_AGG(OBJECT_CONSTRUCT(
                 'qualified_name', t.QUALIFIED_NAME, 'domain', t.DOMAIN_PATH, 'name', t.NAME, 'scope', t.SCOPE,
                 'kind', t.KIND, 'definition', t.DEFINITION, 'synonyms', t.SYNONYMS, 'formula', t.FORMULA,
                 'exclusions', t.EXCLUSIONS, 'version', t.VERSION_NO, 'approved_by', t.APPROVED_BY,
                 'approved_at', t.APPROVED_AT, 'steward_role', t.STEWARD_ROLE, 'score', t.SCORE,
                 'implemented_by', r.R, 'policies', p.P, 'relationships', e.E))
               WITHIN GROUP (ORDER BY t.SCORE DESC, t.QUALIFIED_NAME))
      FROM top_hits t
      LEFT JOIN reps  r ON r.QUALIFIED_NAME = t.QUALIFIED_NAME
      LEFT JOIN pols  p ON p.TARGET_QNAME   = t.QUALIFIED_NAME
      LEFT JOIN edges e ON e.QN             = t.QUALIFIED_NAME
  );
  RETURN res;
END;
$$;

CREATE OR REPLACE PROCEDURE GET_CONCEPT(QUALIFIED_NAME STRING)
  RETURNS VARIANT
  LANGUAGE SQL
  COMMENT = 'Full approved record for one domain-qualified concept, with policies, edges, implementations and history'
  EXECUTE AS CALLER
AS
$$
DECLARE
  qn  STRING DEFAULT TRIM(COALESCE(:QUALIFIED_NAME, ''));
  n   INTEGER;
  res VARIANT;
BEGIN
  n := (SELECT COUNT(*) FROM {{DB}}.API.V_CONCEPT WHERE UPPER(QUALIFIED_NAME) = UPPER(:qn));
  IF (n = 0) THEN
    -- Not qualified (or not visible): return candidate meanings instead of guessing.
    res := (SELECT OBJECT_CONSTRUCT(
              'found', FALSE,
              'message', 'No approved concept with that qualified name is visible to you.',
              'candidates', ARRAY_AGG(QUALIFIED_NAME) WITHIN GROUP (ORDER BY QUALIFIED_NAME))
              FROM {{DB}}.API.V_CONCEPT
             WHERE UPPER(NAME) = UPPER(SPLIT_PART(:qn, '.', -1)) OR UPPER(NAME) = UPPER(:qn));
    RETURN res;
  END IF;

  res := (
    SELECT OBJECT_CONSTRUCT(
             'found', TRUE,
             'qualified_name', c.QUALIFIED_NAME, 'domain', c.DOMAIN_PATH, 'kind', c.KIND, 'scope', c.SCOPE,
             'definition', c.DEFINITION, 'synonyms', c.SYNONYMS, 'formula', c.FORMULA,
             'exclusions', c.EXCLUSIONS, 'formula_provenance', c.FORMULA_PROVENANCE,
             'version', c.VERSION_NO, 'approved_by', c.APPROVED_BY, 'approved_at', c.APPROVED_AT,
             'owner_role', c.OWNER_ROLE, 'steward_role', c.STEWARD_ROLE, 'has_pending_edit', c.HAS_PENDING_EDIT,
             'implemented_by', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT('type', REP_TYPE, 'asset_kind', ASSET_KIND,
                                       'asset', ASSET_FQN, 'member', ASSET_MEMBER, 'validation_status', VALIDATION_STATUS))
                                  FROM {{DB}}.API.V_REPRESENTATION WHERE UPPER(QUALIFIED_NAME) = UPPER(:qn)),
             'policies', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT('policy', r.SOURCE_QNAME, 'rule', p.DEFINITION))
                            FROM {{DB}}.API.V_RELATIONSHIP r JOIN {{DB}}.API.V_CONCEPT p ON p.QUALIFIED_NAME = r.SOURCE_QNAME
                           WHERE r.REL_TYPE = 'APPLIES_TO' AND UPPER(r.TARGET_QNAME) = UPPER(:qn)),
             'outgoing', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT('type', COALESCE(CUSTOM_LABEL, REL_TYPE), 'concept', TARGET_QNAME))
                            FROM {{DB}}.API.V_RELATIONSHIP WHERE UPPER(SOURCE_QNAME) = UPPER(:qn)),
             'incoming', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT('type', COALESCE(CUSTOM_LABEL, REL_TYPE), 'concept', SOURCE_QNAME))
                            FROM {{DB}}.API.V_RELATIONSHIP WHERE UPPER(TARGET_QNAME) = UPPER(:qn) AND REL_TYPE <> 'APPLIES_TO'),
             'history', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT('version', VERSION_NO, 'status', STATUS, 'reviewed_by', REVIEWED_BY,
                                                           'reviewed_at', REVIEWED_AT, 'note', CHANGE_NOTE))
                                WITHIN GROUP (ORDER BY VERSION_NO DESC)
                           FROM {{DB}}.API.V_CONCEPT_HISTORY
                          WHERE UPPER(QUALIFIED_NAME) = UPPER(:qn) AND STATUS IN ('APPROVED', 'SUPERSEDED')))
      FROM {{DB}}.API.V_CONCEPT c
     WHERE UPPER(c.QUALIFIED_NAME) = UPPER(:qn)
  );
  RETURN res;
END;
$$;
