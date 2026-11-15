# RAG design

## Scope: contextual knowledge, not live conditions

| Question | Source |
| --- | --- |
| What is the wave height / current / wind now or forecast? | **Structured connectors** (`divesafe.data`) |
| Is there an active marine warning? | **Structured connectors** |
| What hazards exist at this site? Where are the entry points? | **RAG** |
| What do local regulations and best practice say? | **RAG** |
| How should a given swell direction be interpreted at this site? | **RAG** |
| What happened on similar past days? | **RAG** / historical data service |

Live numeric conditions are never taken from retrieved text. Retrieved text may *interpret*
conditions but cannot supply them. Reason: retrieved documents are stale by nature, and
conditions need typed, timestamped, validated values the rules engine can check.

## Corpus

Dive-site characteristics, site-specific hazards, local regulations, diving best practice, marine
safety guidance, environmental interpretation guidance. Historical-observation summaries are
reserved for a future typed history service (ADR 0022 decision 5), not the first corpus.
Every document needs: `source`, `version/date`, `site_id` (if site-specific), `license/provenance`.
No document without provenance is ingested.

## Architecture

- Store: PostgreSQL (one database for relational and search data). The first version returns all
  approved, in-date chunks for the site with no search and no embeddings (ADR 0022, proposed);
  search and pgvector are later decisions.
- Chunks keep metadata (document, section, site, date) so results can become `EvidenceItem`s
  with `category` of `site_information` or `local_guidance` only (ADR 0022).
- Retrieval filters by `site_id`, document status, version and review-by date. Version 1 has no
  similarity ranking.
- If embeddings are added later, the model sits behind an interface like the LLM provider; the
  model name and version are stored with each chunk, and re-embedding is a documented migration.

## Rules

1. Retrieved passages are evidence with IDs and are cited like any other evidence.
2. Retrieved content is untrusted input: it is delimited in prompts and cannot change
   instructions (prompt-injection defense).
3. Retrieval that finds nothing relevant is reported as such; the agent must not fill in.
4. Retrieval cannot lower severity. A "site is usually fine" passage never overrides rules.
5. Contradictions between documents are surfaced, not resolved silently.
6. Retrieved knowledge reaches a model prompt only under egress basis `declared_local`, or
   `remote_knowledge_opt_in` (the one named host in `DIVESAFE_LLM_KNOWLEDGE_HOST`, ADR 0028);
   under any other basis every `kb:` item is withheld and listed in `withheld_knowledge`
   (`agents.stage.split_for_egress`, ADR 0022 decision 15). Under `remote_knowledge_opt_in` the
   run's recorded basis is passed to every `retrieve` call, and a family whose governing version
   lacks the `attests_model_host_egress` attestation returns nothing, with no fall-back to an
   older version, and the fixed issue `knowledge store: a document version is not approved for
   the model host`. This is applied in both SQL and code, so the disagreement check stays quiet.

## Evaluation

Retrieval recall/precision on a labelled question set per site, plus checks that cited
passages support the claims made (see [evaluation-strategy.md](evaluation-strategy.md)).
