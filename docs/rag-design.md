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

Dive-site characteristics, site-specific hazards, local regulations, diving best practice,
historical observations (summaries), marine safety guidance, environmental interpretation guidance.
Every document needs: `source`, `version/date`, `site_id` (if site-specific), `license/provenance`.
No document without provenance is ingested.

## Architecture

- Store: PostgreSQL + pgvector (one database for relational and vector data).
- Chunks keep metadata (document, section, site, date) so results can become `EvidenceItem`s
  with `category` of `site_information`, `local_guidance` or `historical_observations`.
- Retrieval filters by `site_id` and document status before vector similarity.
- The embedding model sits behind an interface like the LLM provider; the model name and
  version are stored with each chunk, and re-embedding is a documented migration.

## Rules

1. Retrieved passages are evidence with IDs and are cited like any other evidence.
2. Retrieved content is untrusted input: it is delimited in prompts and cannot change
   instructions (prompt-injection defense).
3. Retrieval that finds nothing relevant is reported as such; the agent must not fill in.
4. Retrieval cannot lower severity. A "site is usually fine" passage never overrides rules.
5. Contradictions between documents are surfaced, not resolved silently.

## Evaluation

Retrieval recall/precision on a labelled question set per site, plus checks that cited
passages support the claims made (see [evaluation-strategy.md](evaluation-strategy.md)).
