-- =============================================================================
-- 11  Cortex Agent grounded on the Business Ontology
--     Each source has one job:
--       business_ontology tools  -> what a term MEANS (approved, role-filtered)
--       semantic-view analysts   -> the governed NUMBER
--     Tools run with the caller's role, so ontology search is filtered by the
--     caller's domain privileges.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE SCHEMA {{DB}}.AGENTS;

CREATE OR REPLACE AGENT ONTOLOGY_AGENT
  COMMENT = 'Answers business questions using steward-approved ontology meaning + governed semantic-view metrics'
  PROFILE = '{"display_name": "Governed Analyst", "color": "blue"}'
  FROM SPECIFICATION
$$
models:
  orchestration: auto

orchestration:
  budget:
    seconds: 90
    tokens: 32000

instructions:
  orchestration: |
    You answer business questions on behalf of the user. Before computing anything, decide what each
    business term in the question MEANS.

    1. Meaning first. For every business term (metric, entity, policy) call search_business_ontology.
       The ontology holds steward-approved, domain-qualified definitions (e.g. Finance.Net Revenue).
       Never invent a definition and never prefer a column name over an approved definition.
    2. Ambiguity. If search returns more than one approved concept with the same name in different
       domains (see ambiguous_terms) and the user's wording or prior turns do not clearly select one,
       STOP and ask which one they mean. List each option as "<Domain>.<Name>: <definition>".
       If context does select one, say which one you used and why.
    3. Details. Call get_concept_details on the chosen concept to read its policies (APPLIES_TO),
       exclusions, formula and implemented_by representations.
    4. Numbers. If the concept has an IMPLEMENTS representation on a semantic-view metric, answer with
       the analyst tool for that semantic view and that metric:
         FINANCE_REVENUE_SV -> finance_revenue_analyst
         SALES_BOOKINGS_SV  -> sales_bookings_analyst
       Apply the concept's policies (e.g. use the recognition date for Finance revenue periods).
       Use the ontology formula only as a cross-check; the semantic view is authoritative for the number.
    5. If no approved concept exists, say so plainly, and only then fall back to an analyst tool,
       labelling the result exploratory.
  response: |
    Be concise. Always end with a "Grounding" section containing:
      - Definition: <qualified name> v<version>, approved by <approver> — <definition>
      - Policies applied: <policy names or "none">
      - Exclusions: <list or "none">
      - Number: GOVERNED (semantic-view metric <SV>.<TABLE.METRIC>), DERIVED (show formula over governed
        metrics) or EXPLORATORY (show the SQL).
    Never present a number without its label.
  sample_questions:
    - question: "What was net revenue last quarter?"
    - question: "What was Finance net revenue by region in Q3 2026?"
    - question: "How do Finance and Sales define net revenue differently?"
    - question: "Which policies apply to Finance.Net Revenue?"

tools:
  - tool_spec:
      type: generic
      name: search_business_ontology
      description: >
        Search steward-approved business concepts (metrics, terms, entities, policies) the caller is allowed
        to see. Returns domain-qualified names, kinds, definitions, synonyms, formulas, exclusions, policies,
        relationships, implementing semantic-view metrics and an ambiguous_terms list.
      input_schema:
        type: object
        properties:
          query:
            type: string
            description: Business term or the user's question, e.g. "net revenue".
          domain_filter:
            type: string
            description: Optional domain path to restrict results, e.g. "Finance". Leave empty to search all domains.
          kind_filter:
            type: string
            description: Optional kind filter (TERM, METRIC, ENTITY, ACTION, POLICY, DIMENSION, MEASURE).
          max_results:
            type: integer
            description: Maximum results (default 8).
        required: [query]
  - tool_spec:
      type: generic
      name: get_concept_details
      description: >
        Full approved record for one domain-qualified concept (e.g. "Finance.Net Revenue"): definition, formula,
        exclusions, policies that apply, incoming/outgoing relationships, implementing assets and version history.
      input_schema:
        type: object
        properties:
          qualified_name:
            type: string
            description: Domain-qualified concept name, e.g. "Finance.Net Revenue".
        required: [qualified_name]
  - tool_spec:
      type: cortex_analyst_text_to_sql
      name: finance_revenue_analyst
      description: Governed Finance metrics (GROSS_REVENUE, NET_REVENUE = recognized revenue) by recognition date, region, segment.
  - tool_spec:
      type: cortex_analyst_text_to_sql
      name: sales_bookings_analyst
      description: Governed Sales metrics (GROSS_BOOKINGS, NET_REVENUE = bookings net of discounts at signing) by signed date, region, segment, booking type.

tool_resources:
  search_business_ontology:
    type: procedure
    identifier: {{DB}}.API.SEARCH_CONCEPTS
    execution_environment:
      type: warehouse
      warehouse: {{WAREHOUSE}}
  get_concept_details:
    type: procedure
    identifier: {{DB}}.API.GET_CONCEPT
    execution_environment:
      type: warehouse
      warehouse: {{WAREHOUSE}}
  finance_revenue_analyst:
    semantic_view: {{DB}}.SEMANTIC.FINANCE_REVENUE_SV
    execution_environment:
      type: warehouse
      warehouse: {{WAREHOUSE}}
  sales_bookings_analyst:
    semantic_view: {{DB}}.SEMANTIC.SALES_BOOKINGS_SV
    execution_environment:
      type: warehouse
      warehouse: {{WAREHOUSE}}
$$;

GRANT USAGE ON AGENT ONTOLOGY_AGENT TO ROLE {{CONSUMER_ROLE}};
