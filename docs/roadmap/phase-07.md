# Phase 7: Knowledge

**In short:** Phase 7 let the agent learn from the past. It searches a Contract's own past Reports, reads shared Runbooks, and uses Teachings that the Operator approved. Embeddings came last. They were built although no eval showed a miss.

*Why it matters:* past Reports and approved Teachings can inform a Triage. Knowledge stays inside its Contract, and an unapproved Teaching never reaches a prompt.

**Status:** 7.1, 7.2 and 7.4 are done. 7.3 part a is done. Part b has no date in the source.

| Slice | What | Status |
|---|---|---|
| [7.1](#71-similar-incidents) | Similar incidents: full-text search over past Reports | ✅ done |
| [7.2](#72-shared-knowledge) | Shared knowledge: read-only `shared/` | ✅ done |
| [7.3](#73-teachings) | Teachings: proposals, review queue, prompts | ✅ part a done, part b no date |
| [7.4](#74-embeddings) | Embeddings (pgvector) | ✅ built |

## 7.1 Similar incidents

**Plan:**

- Full-text search over the same Contract's past Reports.
- *Done when:* a Scenario passes where a past Report holds the decisive hint. The isolation test passes.

**Done (2026-10-19):**

- The `incidents` integration has two tools: `incidents_search` and `incidents_read`.
- It uses Postgres full-text search over the Contract's own finished Triage Reports.
- A Triage searches once before its Report.
- Scenario `similar-incident-batch-size` passes on Haiku.

## 7.2 Shared knowledge

**Plan:** a `shared/` folder in the contracts repo, mounted read-only for every Contract.

**Done when:** tests show the agent has no write path to it.

**Done (2026-10-19):**

- It was already `shared/runbooks/`, mounted read-only.
- Tests now prove there is no write path. The agent has read-only tools, no file paths, Runbooks by name only, and read-only mounts.
- Loading refuses a shared file that names a Contract.

## 7.3 Teachings

**What:**

- The agent proposes Teachings after an Investigation.
- It can also propose one from the Operator's thumbs up or down, and from a correction.
- Each proposal is checked against existing Teachings, for duplicates and contradictions.
- Then it goes to a review queue, in Slack and in the console.

**Grilled 2026-10-19:**

- A Teaching is proposed only from a signal. The signal is a 👎 with a correction, or a rejected Proposed action's typed reason.
- It is never proposed automatically after every Triage.
- *Why:* that would mostly restate the Report that Similar incidents already finds.

**Parts:**

- Part a: feedback, drafting, and the review queue.
- Part b: Teachings in the prompts, eval support, and a Scenario.

**Done:**

- Part a was done on 2026-10-20 (`f702f4f`).
- Part b has no date in the source. It adds approved Teachings to the Triage and Question prompts, Scenario `teachings:` support, and Scenario `teaching-restores-from-secret`.

**Done when:**

- an unapproved Teaching never reaches a prompt;
- A's Teachings never reach B.

## 7.4 Embeddings

**Plan:** pgvector embeddings. This was to be built only if 7.1's evals showed misses.

**Built (the Operator's call, 2026-10-20):**

- No eval showed a miss.
- Scenarios replay recorded searches. So no Scenario can show a miss either.

**Grilled:**

- Embeddings come from OpenRouter: `openai/text-embedding-3-small`, with 512 dimensions.
- Hybrid search combines embeddings with full-text search, using reciprocal rank fusion.
- A Contract opts out with `embeddings: false`.
- Embedding tokens count in its usage.
- A Report is embedded when its Triage finishes. `aiops incidents embed` embeds the rest.

**Done when:** a store test finds, by meaning, a past Report that full-text search misses. The isolation test passes.
