# Architecture and design decisions

## 1. Three layers, one job each

| Layer | Job | Authority |
|---|---|---|
| **Business Ontology** (this repo) | What a term *means*, who owns it, which policies apply, which assets implement it | Steward-approved definitions |
| **Semantic views** | How a number is *computed* (joins, filters, aggregation) | Authoritative for any number they cover |
| **Cortex Sense** (optional) | Broad inferred context | Evidence only; promoted findings become drafts |

The ontology stores formulas *as written* and never executes them. A representation is a pointer,
not a computation. The agent uses the semantic-view metric for the number and the ontology formula
only as a cross-check.

## 2. Data model (`CORE`)

```
DOMAIN ─┬─ DOMAIN_ACCESS (privilege → generated role)
        └─ DOMAIN_ANCESTOR (closure table, depth 0 = self)
CONCEPT ── identity: domain, name, scope, kind, status, current/pending version pointers
  └─ CONCEPT_VERSION ── append-only content: definition, synonyms, formula*, exclusions*, provenance*,
                        source, author, reviewer, base_version (stale-edit detection)      * METRIC only
RELATIONSHIP ── source → target, fixed type (+label iff CUSTOM), governed by source domain
REPRESENTATION ── concept → asset (kind, fqn, member), type, baseline snapshot + fingerprint, validation status
DRIFT_FINDING · IMPORT_BATCH · AUDIT_EVENT · SETTINGS
```

Identity is split from content so an approved version keeps serving while an edit is pending,
and history is never rewritten. Qualified names are `Domain.Path.Name [scope]`.

**Lifecycles**

```
concept:  DRAFT ──approve──▶ APPROVED ──retire──▶ RETIRED
            └──reject──▶ REJECTED     └──merge──▶ MERGED (into survivor)
version:  DRAFT|PROPOSED ──▶ APPROVED ──(next approval)──▶ SUPERSEDED ;  ──▶ REJECTED
link:     DRAFT ──▶ APPROVED | REJECTED ──▶ RETIRED
```

## 3. Security model

### Reads: row access policies, evaluated as the caller
`RAP_ONTOLOGY_OBJECT(domain_id, status, author)` is attached to concepts, versions, relationships,
representations, drift findings and audit events; `RAP_DOMAIN` to domains. A row is visible when:

* every **strict ancestor** domain is navigable (caller holds its `USAGE` role) — no inheritance, and
* in the row's own domain the caller holds `MODIFY` (all rows), `READ` (approved/superseded rows), or
  `SUGGEST` and is the row's author (their own drafts).

Anything else behaves as if it does not exist. Domain privilege roles are nested
`MODIFY ⊃ SUGGEST ⊃ READ ⊃ USAGE`, and every `USAGE` role inherits `ONTOLOGY_CONSUMER` (API access).
Consumers get `SELECT` on `API` secure views only; `CORE` and `OPS` are not granted.

Because policies use the querying session, they also apply to **caller's-rights** procedures
(`SEARCH_CONCEPTS`, `GET_CONCEPT`, `EXPORT_ONTOLOGY`) and therefore to Cortex Agent tools, which run
with the caller's role.

### Writes: owner's-rights procedures that authorize the real caller
Callers have no DML. Every write goes through an `EXECUTE AS OWNER` Python procedure. Inside an
owner's-rights procedure, Snowflake evaluates `CURRENT_ROLE()` / `IS_ROLE_IN_SESSION()` against
the **owner**, but `CURRENT_USER()` is the **caller** (verified during build). `authz.py`
therefore walks the grant graph upward from the domain privilege role (`SHOW GRANTS OF ROLE`)
until it reaches the calling user. Consequences:

* Authorization is based on roles **granted** to the user (directly or via hierarchy), not only the
  currently active role. This matches "steward = person holding the steward role".
* The service role needs `MANAGE GRANTS` to see grants of business roles it does not own.
* Admin (`ONTOLOGY_ADMIN`) can create domains and run maintenance but is **not** an approver unless
  it is also granted a domain's `MODIFY` role — approval belongs to the domain steward.
* `REQUIRE_DISTINCT_APPROVER` (default on) blocks authors approving their own changes.

All dynamic identifiers (role names, asset FQNs, domain segments) are validated with strict
regexes before use; all values are bound parameters.

### Transactions
Each procedure runs in one transaction and writes its `AUDIT_EVENT` row in that transaction. Role
DDL in `CREATE_DOMAIN` auto-commits (Snowflake semantics); it is idempotent, and the metadata
writes that follow run in a fresh transaction.

## 4. Integrity rules (enforced in `validation.py`)

* Only `METRIC` concepts may carry formula, exclusions, formula provenance.
* `CUSTOM` edges require an `UPPER_SNAKE` label; other types may not have one.
* `EQUIVALENT_TO` is cross-domain only and symmetric (no reverse duplicate); same-domain duplicates are merged.
* `APPLIES_TO` must originate from a `POLICY`; `DERIVES` must target a `METRIC`/`MEASURE`.
* `IS_A`, `HAS_PART`, `DERIVES` are acyclic.
* Representations must reference an existing, visible asset; `BI_ASSET` cannot `IMPLEMENT`;
  `RELATED_DASHBOARD` is BI-only; members are required for columns and semantic-view members.
* Link approval requires all linked concepts to be approved.
* Merge is same-domain and same-kind; edges are re-pointed (dropping self-loops/duplicates), the
  duplicate's name and synonyms are added to the survivor as a new approved version.

## 5. Drift

On proposal a representation stores a snapshot of its asset (expression, comment, data type,
schema). `OPS.T_VALIDATE_REPRESENTATIONS` re-snapshots every live representation and records
`ASSET_MISSING | FORMULA_CHANGED | DESCRIPTION_CHANGED | TYPE_CHANGED | SCHEMA_CHANGED` findings.
Baselines advance only when a steward `ACCEPT`s, so drift never silently heals.

## 6. Retrieval and the agent

`SEARCH_CONCEPTS` scores exact name, qualified name, contained name/synonym, definition text and
Jaro-Winkler similarity; returns policies (`APPLIES_TO`), edges, implementations and an
`ambiguous_terms` list when several visible concepts share a name. It is a deterministic SQL
procedure rather than a Cortex Search service on purpose: a search service runs as its owner and
would return definitions the caller is not entitled to.

The agent's instructions give each tool one job (ontology = meaning, semantic view = number), make it
ask when a term is ambiguous for *this caller*, and require a grounding block labelling each number
GOVERNED, DERIVED or EXPLORATORY.

## 7. Known limits

* Snowflake does not enforce PK/FK; integrity relies on procedures being the only writers.
* Authorization reflects granted roles, not the active role.
* `SEARCH_CONCEPTS` is lexical. For very large ontologies, add a per-domain Cortex Search service
  and keep the RAP-filtered procedure as the final filter.
* The importer handles a practical SKOS subset (prefLabel, altLabel, definition, broader, related,
  exactMatch, `ont:kind`, `ont:formula`).
