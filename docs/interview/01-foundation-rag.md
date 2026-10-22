# Interview Prep 01 — Foundation + LLM + RAG (Day 1)

Every answer is grounded in the Day 1 code. Format: direct answer, then the reasoning, then a code
pointer. Companion: `docs/journal/01-foundation-rag.md`.

---

## Basic

### Q1. What does a request to `POST /api/v1/rag/answer` do, end to end?
**Answer:** It authenticates the API key to a tenant, runs hybrid retrieval for that tenant, packs
the best chunks into a character budget, asks `qwen3:8b` for a JSON answer with citations,
validates the citations, and returns a `GroundedAnswer` with status, citations, usage, cost and
timings.
Steps: `deps.get_principal` → `Answerer.answer` → `HybridRetriever.search` (vector + keyword, RRF,
dedupe) → `select_evidence` (≤ `max_evidence_chars`=8000, at least one chunk) → `build_messages`
(fenced `<source id="Sn">` blocks) → `LLMRouter.chat("main", json_schema=AnswerDraft)` →
`validate_draft` → at most one correction → map `Sn` back to chunk/document/page.
Pointer: `app/api/retrieval.py:answer`, `app/rag/answer.py:Answerer.answer`.

### Q2. What is Reciprocal Rank Fusion and how is it implemented here?
**Answer:** RRF scores each document as the sum over retrievers of `1 / (k + rank)`, with k=60, and
sorts by that sum.
It uses only ranks, so the vector retriever's cosine similarity and the keyword retriever's
`ts_rank_cd` never need to be put on the same scale. A chunk ranked by both retrievers beats a
chunk ranked first by only one (unit test: c at vector #3 + keyword #1 scores `1/63 + 1/61` and
beats a, which is vector #1 only). Ties are broken by chunk ID for determinism. Each fused result
keeps `vector_rank/score` and `keyword_rank/score` for diagnostics.
Pointer: `app/retrieval/hybrid_search.py:reciprocal_rank_fusion`;
`tests/unit/test_retrieval_and_answer.py::test_rrf_rewards_agreement_between_retrievers`.

### Q3. What are the four document statuses and what does each guarantee?
**Answer:** `pending` (stored, waiting), `processing` (chunking/embedding), `ready` (every chunk
embedded and committed), `failed` (zero searchable chunks, `error` set).
The key invariant is that READY and FAILED are never partial: chunks are only visible with READY,
and the failure path deletes chunks and sets `chunk_count=0`. Retrieval only reads READY + latest
documents.
Pointer: `app/db/models.py:DocumentStatus`; `app/ingestion/pipeline.py:process_document`;
`app/retrieval/vector_search.py:scoped_select`.

### Q4. How does the system decide whether an upload is a duplicate or a new version?
**Answer:** Duplicate = same tenant and same SHA-256 of the bytes; new version = same tenant and
same filename but different bytes.
`create_document` looks up `(tenant_id, content_hash)` first and returns the existing doc with
`created=False` (API returns 200 and `duplicate: true`, no background job). Otherwise `version =
max(version for filename) + 1`. Both are backed by unique constraints
(`uq_documents_tenant_hash`, `uq_documents_tenant_file_ver`); a race that loses on the constraint
re-reads by hash or raises `ConflictError` (409).
Pointer: `app/ingestion/pipeline.py:create_document`; `app/api/documents.py:upload_document`.

### Q5. What file validation happens before anything is stored?
**Answer:** Size limit (10 MB, read `limit+1` bytes to detect overflow), extension allowlist,
content checks per type, normalization, and a non-empty check.
PDFs must start with `%PDF-` and must not be encrypted; text files must contain no NUL byte and be
UTF-8. Filenames are reduced to a basename with control characters removed. If the normalized text
is empty, `EmptyDocumentError` (422) is raised, with an OCR hint for PDFs. All of this is in the
request path, so a bad file is never written to the database.
Pointer: `app/ingestion/parsers.py:extract_text`, `sanitize_filename`;
`app/api/documents.py:upload_document`.

### Q6. What does the structured chunker do differently from the fixed one?
**Answer:** Structured packs whole paragraphs, starts a new chunk at a Markdown heading once the
current chunk is at least half full, splits only oversized paragraphs (by sentence, then by
words), and labels each chunk with its section(s). Fixed is a sliding window cut at a space.
Both return exact `(char_start, char_end)` spans into the normalized text, so page numbers come
from counting `\f` before the offset. Overlap in structured mode carries trailing whole units, but
not across a heading-aligned break. Fixed has `section=None`.
Pointer: `app/ingestion/chunking.py:_structured`, `_fixed`, `_split_large`.

### Q7. How is cost computed for a model call?
**Answer:** Local providers (`ollama`, `fake`) cost `$0` with `pricing_source="local"`; hosted
models use `HOSTED_PRICES` if the model is listed, otherwise `cost_usd=None` and
`pricing_source="unavailable"`.
The table is empty on purpose: prices are not invented. Every cost carries `PRICING_VERSION` so
historical numbers can be interpreted later.
Pointer: `app/llm/usage.py:estimate_cost`.

### Q8. What does `/ready` check that `/health` does not?
**Answer:** `/health` only says the process is up; `/ready` checks the database (`SELECT 1`),
Ollama reachability, and that every configured model (main, small, embedding) is installed,
returning 503 with `missing_models` otherwise.
That split matters for orchestration: liveness failures restart the pod, readiness failures only
remove it from load balancing.
Pointer: `app/api/health.py:health`, `ready`.

### Q9. What is a chunk's `section` label when a chunk spans several headings?
**Answer:** All distinct sections of the chunk's new (non-overlap) units, joined with `" | "`,
capped at 500 chars, e.g. `"A | B"`.
This was a Day 1 bug: the label used to be the first heading only, so a chunk containing
"Retention" content was labelled "Recovery". Because the label is also embedded
(`embedding_input`) and indexed in `tsv`, a wrong label hurt retrieval and citations.
Pointer: `app/ingestion/chunking.py:_structured.flush`;
`tests/unit/test_chunking.py::test_chunk_spanning_sections_lists_all_of_them`.

---

## Logical

### Q10. Why does keyword search rewrite `&` to `|` in the tsquery? What was the bug?
**Answer:** Because `websearch_to_tsquery` ANDs every non-stopword term, and natural-language
questions almost never have every term in one chunk, keyword search returned 0 candidates.
Detected in a live run from `RetrievalDiagnostics.keyword_candidates: 0`. "how long are transaction
records kept" became `'long' & 'transact' & 'record' & 'kept'`. The fix casts the tsquery to text,
replaces `' & '` with `' | '`, and casts back. `ts_rank_cd` still ranks chunks matching more terms
higher, and RRF combines it with vector rank. After the fix, hybrid surfaced chunks found by both
retrievers. Phrases survive because they compile to `<->`, not `&`.
Pointer: `app/retrieval/keyword_search.py:keyword_search`;
`tests/integration/test_retrieval.py::test_keyword_search_matches_natural_language_questions`.

### Q11. Is that OR rewrite safe for every query?
**Answer:** Not on its own — negation inverts it, and that was a real bug, now fixed.
`failover -patroni` parses to `'failov' & !'patroni'`; a blind `&`→`|` rewrite gives
`'failov' | !'patroni'`, which matches nearly every chunk. Ranking hid it (failover chunks still
came first), but the candidate set ballooned. The fix (`split_negations`) pulls `-term` and
`-"phrase"` out of the query before parsing, OR-rewrites only the positive part, and applies the
exclusions as a separate `NOT tsv @@ websearch_to_tsquery(...)` filter. Live, `failover
-patronictl` now returns 2 candidates. Hostile syntax is otherwise safe: all user text is a bound
parameter and `websearch_to_tsquery` never raises (`test_keyword_search_handles_hostile_query_syntax`).
Guards: `test_negated_terms_exclude_rather_than_match_everything`, `test_split_negations`.
Pointer: `app/retrieval/keyword_search.py`.

### Q12. Why strip zero-width characters during normalization? What broke without it?
**Answer:** A file containing only U+200B was accepted as non-empty, because Python's
`str.strip()` does not treat zero-width space, word joiner, or BOM as whitespace.
The document would have gone to the background pipeline and failed there (or produced a
meaningless chunk) instead of a fast 422. The same characters can hide text from a human reviewer
while the model still reads it, so they are a prompt-injection hiding vector. Fix:
`_INVISIBLE = [​⁠﻿]` removed in `normalize_text`.
Pointer: `app/ingestion/parsers.py:normalize_text`;
`tests/unit/test_parsers.py::test_empty_documents` (parametrized with `"​"`).

### Q13. Why is the old version demoted only after the new one is READY, and what was the reindex bug?
**Answer:** So there is never a moment where neither version is searchable, and a failed new
version never hides a good old one. The bug: reindexing v1 after v2 existed could have promoted v1
back to `is_latest`.
Demotion of siblings and promotion of self happen inside the same transaction that inserts the
chunks. The fix counts READY versions of the same filename with a higher `version`; if any exist,
siblings are not demoted and this doc gets `is_latest=False`.
Pointer: `app/ingestion/pipeline.py:process_document` (`newer_ready`);
`tests/integration/test_ingestion.py::test_new_content_same_filename_creates_new_version`.

### Q14. What breaks if you remove the `Document.tenant_id == tenant_id` condition from `scoped_select`, given chunks already have `tenant_id`?
**Answer:** Nothing today, and that is the point: it is defense in depth. Chunks denormalize
`tenant_id` from their document; if a bug ever wrote a chunk with the wrong tenant, the second
condition still blocks it.
`scoped_select` is the single base query for vector and keyword search, so isolation cannot be
forgotten by one retriever. The integration test also passes tenant A's document IDs as a filter
from tenant B and asserts zero results.
Pointer: `app/retrieval/vector_search.py:scoped_select`;
`tests/integration/test_retrieval.py::test_tenant_isolation`.

### Q15. Why does `process_document` commit PROCESSING in one session, then do embedding outside any transaction, then open a new transaction?
**Answer:** To avoid holding a DB connection and an open transaction during slow network calls
to the embedder.
Embedding a document can take seconds; holding a pooled connection (pool_size=10) for that would
starve the API. The read session closes after copying `filename`, `tenant_id`, `extracted_text`,
`version`; the write transaction only does deletes/inserts/updates.
Pointer: `app/ingestion/pipeline.py:process_document`; `app/db/session.py:make_engine`.

### Q16. Why does the ordering "retry inside each route, then fall back to the next route" matter?
**Answer:** A brief blip on the local model should be retried locally before sending data to a
hosted provider, and permanent errors must stop immediately.
`LLMRouter.chat` wraps each route in `retry_async(max_retries)`. Only if the route still fails
with a transient error does it move to the next. A non-transient error (e.g. 4xx "model not
found") is re-raised at once with no retry and no fallback. `attempts` counts every call
(test: 2 primary + 1 backup = 3), and `fallback_used` is set when index > 0.
Pointer: `app/llm/provider.py:LLMRouter.chat`;
`tests/unit/test_llm.py::test_router_falls_back_on_transient_error`,
`test_router_does_not_retry_or_fall_back_on_permanent_error`.

### Q17. What happens if the model cites `[S42]` when only S1–S3 exist?
**Answer:** `validate_draft` flags "cites sources that do not exist", the answerer sends one
correction turn listing valid IDs, and if the second reply is still invalid the status is
`failed_validation` with no citations and a generic safe message.
It checks both the `citations` array and inline `[Sn]` markers. It also rejects answers with no
citations unless `insufficient_evidence` is true, and empty answers. The invalid text is never
returned.
Pointer: `app/rag/answer.py:validate_draft`, `Answerer.answer`;
`tests/integration/test_api.py::test_invalid_citations_get_one_correction_then_fail`,
`test_correction_can_recover`.

### Q18. Why does `select_evidence` always keep at least one chunk even if it exceeds the budget?
**Answer:** Because returning zero evidence would trigger the no-evidence short-circuit and claim
"nothing found" when the best match simply happens to be long.
It stops at the first chunk that would overflow (preserving rank order rather than packing
smaller lower-ranked chunks).
Pointer: `app/rag/answer.py:select_evidence`;
`tests/unit/test_retrieval_and_answer.py::test_select_evidence_respects_budget_but_keeps_one`.

### Q19. Re-upload v1's exact bytes after v2 is live. What happens?
**Answer:** Nothing changes: the upload matches v1 by `(tenant, content_hash)`, returns v1 with
`duplicate: true`, and search keeps serving v2.
This is a real edge case of hash idempotency combined with filename versioning: "revert to the old
content" is silently a no-op. Same for re-uploading a FAILED doc's bytes: you get the failed doc
back and must call `/reindex`. Fixing it means treating a hash match on a non-latest version as a
new version or an explicit "promote" operation.
Pointer: `app/ingestion/pipeline.py:create_document`, `_by_hash`.

---

## Advanced

### Q20. The access log showed `tenant_id: null`. Why, how was it fixed, and is it fully fixed?
**Answer:** A ContextVar set during dependency resolution is not visible to the middleware's
context afterwards, so the access log read `None`. Fix: `get_principal` also writes
`request.state.tenant_id` and the middleware reads that. The access log is fixed; in-endpoint log
lines are not.
Context changes only flow down into child tasks/copies, never back up. The middleware's
`call_next` runs the app in a different context. Request state is a plain object shared by
reference, so it works. A second instance of the same bug was found
afterwards: `get_principal` was a sync `def`, which FastAPI runs in a threadpool with a *copied*
context, so its `tenant_id_var.set` never reached the endpoint and "document created" logs carried
`tenant_id: null`. Fixed by making it `async def` (runs in the request's own context); guarded by
`test_logs_inside_endpoints_carry_tenant`, which was confirmed to fail with the sync version.
Pointer: `app/api/deps.py:get_principal`; `app/main.py:request_context`;
`tests/integration/test_api.py::test_access_log_includes_tenant`.

### Q21. Why is `hnsw.iterative_scan = strict_order` set, and what does `ef_search` do here?
**Answer:** HNSW applies WHERE clauses after the index scan. With a selective tenant filter, the
`ef_search` candidates may mostly belong to other tenants, and the query returns fewer than
`limit` rows. Iterative scan (pgvector ≥ 0.8) keeps scanning until enough filtered rows exist.
`ef_search` is the candidate list size; it is set to `max(40, min(limit*2, 1000))` so hybrid's 30
candidates get 60. `strict_order` keeps exact distance ordering (vs `relaxed_order`, which is
faster but may reorder). `SET LOCAL` scopes both to the retrieval transaction, so pooled
connections are not polluted. The f-string is safe because the value is an int computed from a
bounded `limit`.
Pointer: `app/retrieval/vector_search.py:vector_search`.

### Q22. What happens if the process crashes mid-ingestion?
**Answer:** The document stays `processing` forever with no chunks; it is not searchable and
nothing retries it.
`BackgroundTasks` runs in the API process after the response; there is no durable queue, lease, or
sweeper. Because chunks + READY are one transaction, there is no partial index, only a stuck
status. `mark_for_reindex` refuses `pending/processing` (409), so the user cannot even retry
through the API. Fix path: durable queue (`SKIP LOCKED` job table) plus a sweeper that resets
stale `processing` rows by `updated_at`.
Pointer: `app/api/documents.py:upload_document`; `app/ingestion/pipeline.py:mark_for_reindex`.

### Q23. How does the system defend against prompt injection inside documents?
**Answer:** Three layers: invisible characters are stripped at ingestion, evidence is fenced as
`<source>` blocks whose closing tags cannot be forged, and the system prompt declares evidence
untrusted and forbids following it. Output is then constrained by JSON schema and citations are
validated.
`_escape` rewrites `<source`, `</source`, `<evidence` inside document text to `[source…]`, so a
document cannot close the fence and pose as a new source or as instructions (unit test asserts
exactly one `<source ` and one `</source>`). This reduces but does not eliminate injection: the
model can still be persuaded by text inside the fence. Day 1 has no tools, so the blast radius is
the answer text; Day 2's policy gate is the real control once tools exist.
Pointer: `app/ingestion/parsers.py:_INVISIBLE`; `app/rag/answer.py:_escape`, `build_messages`,
`SYSTEM_PROMPT`; `tests/unit/test_retrieval_and_answer.py::test_evidence_is_fenced_and_cannot_break_out`.

### Q24. What are the failure modes of reindexing a document?
**Answer:** The document disappears from search while `pending/processing`, and if reindex fails,
its previous good chunks are deleted and it stays FAILED with `is_latest=False`.
`scoped_select` requires READY, `mark_for_reindex` sets PENDING, and the failure path in
`process_document` deletes chunks. This is fail-closed (never serve stale or partial data) at the
price of availability. An older version is not re-promoted. Blue/green indexing (new
generation, atomic pointer flip) would remove both gaps. Also `mark_for_reindex` is
check-then-update, so two concurrent reindex calls can both schedule work (idempotent result, wasted
compute).
Pointer: `app/ingestion/pipeline.py:mark_for_reindex`, `process_document`;
`tests/integration/test_ingestion.py::test_failed_reindex_leaves_no_partial_chunks`.

### Q25. What would you monitor in production for this pipeline?
**Answer:** Retrieval: `keyword_candidates`/`vector_candidates` (zero is a smell, as bug 2
showed), `duplicates_removed`, per-stage ms. Answering: status distribution
(`failed_validation`, `insufficient_evidence` rates), `correction_attempts`, tokens, generation
ms, `fallback_used`, `attempts`. Ingestion: count and age of `processing`/`failed` documents.
All of these are already emitted: `RetrievalDiagnostics` and `GroundedAnswer` return them per
request; JSON logs carry request ID; documents have `status`, `error`, `updated_at`. What is
missing is aggregation (metrics/traces), which is Day 3.
Pointer: `app/retrieval/schemas.py:RetrievalDiagnostics`; `app/rag/answer.py:GroundedAnswer`;
`app/core/logging.py:JsonFormatter`.

### Q26. How are secrets kept out of logs and error responses?
**Answer:** `AppError` separates client-safe `message` from log-only `detail`; validation errors
return locations and messages, never submitted values; the JSON formatter redacts sensitive keys
and patterns recursively; keys are logged only as a 12-hex fingerprint.
`redact` masks keys matching `api_key|authorization|password|secret|token|cookie`, values like
`Bearer …`, `sk-…`, and the password in Postgres DSNs. `unhandled` returns a generic 500. Settings
use `SecretStr`.
Pointer: `app/core/errors.py:AppError`; `app/core/logging.py:redact`; `app/main.py:validation_error`;
`app/core/security.py:_fingerprint`; `tests/unit/test_core.py::test_redaction`.

---

## Scenario

### Q27. A tenant reports seeing another tenant's document. Walk through your response.
**Answer:** Treat it as a security incident: confirm with the request ID, identify which path
returned the data, and contain before root-causing.
1. Get the `X-Request-ID` from the user; the access log line has `tenant_id`, path, status.
2. Classify the path. Document CRUD filters by `Document.tenant_id` in `IngestionService.get/
   list/delete`; retrieval goes through `scoped_select`, which filters both chunk and document
   tenant. A leak therefore means one of: a key mapped to the wrong tenant in
   `AGENTOPS_API_KEYS` (most likely: config, not code), a new query path that bypassed
   `scoped_select`, or chunk rows with a wrong `tenant_id` *and* a document with a wrong tenant.
3. Check the key: `parse_api_keys` maps each key to exactly one tenant; a duplicated key with a
   different tenant would silently overwrite in the dict. Check fingerprint `key_id`.
4. SQL check: `SELECT … FROM document_chunks c JOIN documents d ON … WHERE c.tenant_id <>
   d.tenant_id` should be empty.
5. Add a regression test alongside `test_tenant_isolation` / `test_cross_tenant_access_is_not_found`.
Longer term: Postgres RLS as a second wall.
Pointer: `app/retrieval/vector_search.py:scoped_select`; `app/core/security.py:parse_api_keys`.

### Q28. Retrieval quality dropped after changing chunk size. How do you diagnose and fix it?
**Answer:** First confirm what actually changed (only newly ingested/reindexed docs use the new
config), then compare old vs new on a fixed question set, then roll back or reindex everything
consistently.
- `Document.chunking_config` records `strategy:size:overlap` per document, so a mixed index
  (some docs at 1200, some at 400) is visible with a query. In a mixed index, documents compete
  with different chunk granularities, which makes results hard to reason about.
- Small chunks lose context (section text separated from its numbers); large chunks dilute the
  embedding and eat the 8000-char budget so fewer sources fit. Overlap must stay `< size`
  (`Settings._check`).
- Use `/retrieval/search` with `mode=vector|keyword|hybrid` and look at `vector_rank`,
  `keyword_rank`, candidate counts.
- Fix: reindex all documents (`/reindex`, which re-chunks from stored `extracted_text`, no
  re-upload). Chunk IDs change with config (`stable_chunk_id` includes `config.key`), so any
  eval labels tied to chunk IDs must be regenerated. The `fixed` strategy exists as a baseline for
  exactly this comparison on Day 4.
Pointer: `app/ingestion/chunking.py:ChunkingConfig.key`; `app/ingestion/pipeline.py:mark_for_reindex`.

### Q29. Ollama is down during a demo. What does the user see, and what can you do?
**Answer:** `/ready` reports `ollama: unreachable` (503). `/rag/answer` fails with 502
`embedding_error` before the LLM is even called, because hybrid retrieval embeds the query first.
Uploads are accepted but go FAILED. Keyword-only search still works.
- `_map_http_error` turns connection errors into `ProviderUnavailableError` (transient);
  `OllamaEmbedder` retries it (default `max_retries=2`, full jitter) and then wraps it as
  `EmbeddingError` (502). The error envelope carries the request ID.
- The hosted LLM fallback does not help here: embeddings have no fallback, and retrieval runs
  before generation. `/llm/chat` alone would use the fallback route if configured (after
  `llm_max_retries=1` local retry).
- `/retrieval/search` with `mode="keyword"` does not touch the embedder, so it keeps working.
- Upload: `create_document` succeeds (no model needed), `process_document` fails → FAILED with
  `error="Embedding failed: …"`, zero chunks. After recovery, `/reindex` it.
- Mitigation for the demo: check `/ready` before starting, keep `ollama serve` warm, and fall back
  to keyword search. A real fix needs an embedding fallback (or cached query embeddings) in
  addition to the LLM fallback, which is itself only mock-tested.
Pointer: `app/llm/provider.py:_map_http_error`, `LLMRouter`; `app/api/health.py:ready`;
`app/services.py:build_router`.

### Q30. 10x more documents. What breaks first?
**Answer:** In order: ingestion throughput (one in-process background task per upload, embedding
at laptop GPU speed), then HNSW memory/recall under selective tenant filters, then reindex
operations that take documents offline.
- Ingestion is bounded by the embedder; `BackgroundTasks` has no concurrency control or
  durability. Move to a queue with N workers.
- `document_chunks` HNSW is a single global index. Build time and RAM grow linearly; filtered
  queries rely on iterative scan, which costs more as one tenant becomes a smaller fraction.
  Partition by tenant or add partial indexes for big tenants.
- GIN on `tsv` scales fine; `ts_rank_cd` over many OR matches gets slower (the OR rewrite
  increases matches).
- Hybrid fetches 30 candidates per retriever regardless of corpus size, so latency of fusion is
  flat; recall per candidate does drop, which is where a reranker or threshold (Day 4) comes in.
- `ApiKeyAuthenticator` is O(keys), irrelevant for documents but relevant for tenants.
Pointer: `app/db/models.py:DocumentChunk.__table_args__`; `app/retrieval/hybrid_search.py:HybridRetriever.search`.

### Q31. A user asks a question the documents do not cover, and gets a confident wrong answer. How is that possible here and how do you prevent it?
**Answer:** Because there is no relevance threshold, vector search always returns the nearest
chunks, so the model gets weak but non-empty evidence; the no-evidence short-circuit does not
fire. The only guard left is the model choosing `insufficient_evidence`.
In the live run "CEO's favorite programming language" the model did return
`insufficient_evidence`, but that is model behaviour, not a guarantee. `validate_draft` checks
that citations exist, not that the cited text supports the claim. Prevention: a calibrated
similarity threshold (Day 4, from the eval set), requiring both retrievers to agree for low
scores, and the Day 2 `verify_answer` step that checks entailment.
Pointer: `app/rag/answer.py:Answerer.answer` (`if evidence:`), `validate_draft`.

---

## Trade-off

### Q32. RRF vs weighted score blending: what was chosen, why, and when would blending win?
**Answer:** RRF. Blending wins when you have labelled data to tune weights and the scores are
calibrated.
Cosine similarity and `ts_rank_cd` with normalization 32 (rank/(rank+1)) are on unrelated
scales with unrelated distributions; a weighted sum needs per-corpus normalization and a tuned alpha, and
silently breaks when the corpus changes. RRF is parameter-light (k=60 from Cormack et al.) and
rewards agreement. It throws away magnitude: a chunk that is far better than the rest in vector
space gets the same credit as a marginal #1. With an eval set (Day 4) a tuned convex combination
or a cross-encoder reranker may beat RRF; the per-result `vector_score/keyword_score` are already
returned to make that experiment possible.
Pointer: `app/retrieval/hybrid_search.py:reciprocal_rank_fusion`.

### Q33. pgvector vs a dedicated vector database?
**Answer:** pgvector, because transactional consistency with document state and SQL filtering
matter more at this scale than raw vector throughput. A dedicated store wins at hundreds of
millions of vectors, very high QPS, or when you need native filtered-HNSW / multi-tenancy
features.
What pgvector buys here: chunks and READY committed atomically (`process_document`), tenant +
status + `is_latest` + JSONB metadata filters in one query (`scoped_select`), full-text search in
the same DB, one backup, one migration tool. What it costs: post-filter HNSW (mitigated with
iterative scan), index RAM on the DB host, and vector load competing with OLTP. The retrieval
layer returns `Candidate` objects, so swapping the vector store touches `vector_search.py` only.
Pointer: `app/retrieval/vector_search.py`; `app/db/models.py:DocumentChunk`.

### Q34. Synchronous ingestion vs two-phase (request + background)?
**Answer:** Two-phase. Fully synchronous wins for CLIs and batch jobs where the caller wants a
final status, which is exactly what `agentops ingest` does by calling both phases inline.
The request phase is cheap and deterministic (validate, extract, hash, store), so all user errors
return 4xx immediately and are never stored. Embedding is slow and network-dependent, so the API
returns 202 with a `Location` header and the client polls. Cost: needs a status model and a
durable worker eventually.
Pointer: `app/ingestion/pipeline.py:create_document`, `process_document`; `app/cli.py:ingest`.

### Q35. 404 vs 403 for another tenant's document?
**Answer:** 404. 403 wins inside a single tenant where the caller is allowed to know the resource
exists (e.g. a viewer trying to delete a doc they can see).
Across tenants, 403 is an existence oracle: an attacker could enumerate valid document IDs.
Returning the same 404 for "missing" and "belongs to someone else" leaks nothing. Tested for GET,
DELETE and reindex.
Pointer: `app/ingestion/pipeline.py:get`, `delete`;
`tests/integration/test_api.py::test_cross_tenant_access_is_not_found`.

---

## Justification

### Q36. "Storing `extracted_text` for every document doubles storage. Why not just re-parse the original?"
**Answer:** The original is not kept at all, and the normalized text is the coordinate system that
every chunk offset and citation refers to. Re-parsing could produce different text (a new pypdf
version, a normalization change) and silently invalidate `char_start/char_end` and page numbers.
It also makes reindex a DB-only operation: change chunk size or embedding model and call
`/reindex`, with no dependency on users re-uploading. The column is `deferred`, so list/get
queries do not load it; only `process_document` uses `undefer`. Storage cost is roughly the size
of the text, which is small next to 768 floats per chunk.
Pointer: `app/db/models.py:Document.extracted_text`; `app/ingestion/pipeline.py:process_document`.

### Q37. "Fallback only on transient errors is too conservative. If the local model gives a bad answer, just ask the hosted one."
**Answer:** A bad answer is a quality problem, not an availability problem, and routing quality
problems to a different vendor hides them and sends tenant data off-machine for no guaranteed
gain.
The quality path is explicit and measurable: `validate_draft` + one correction turn, and the
outcome is reported as `failed_validation`. If the local model is not good enough, that shows up
in eval (Day 4) and the fix is a model/config change, not a hidden per-request escalation. Also,
non-transient errors (4xx, schema errors) would fail identically on the fallback, doubling latency.
Pointer: `app/llm/provider.py:LLMRouter` docstring and `chat`; `app/core/retry.py:is_transient`.

### Q38. "Deterministic chunk IDs are over-engineering. Random UUIDs work."
**Answer:** Random IDs change on every reindex, which breaks anything that refers to a chunk:
stored citations, eval ground truth ("the answer is in chunk X"), and comparisons between runs.
`stable_chunk_id` is uuid5 of document ID, chunking config key, index and text hash, so a re-run
with the same inputs reproduces IDs exactly, and a config change produces disjoint IDs, making
"which config produced this chunk" unambiguous. Cost is one hash per chunk.
Pointer: `app/ingestion/chunking.py:stable_chunk_id`;
`tests/unit/test_chunking.py::test_chunk_ids_are_stable_and_config_sensitive`.

### Q39. "Skipping the model when there is no evidence is pointless; the model would say 'I don't know' anyway."
**Answer:** It is not guaranteed to, a generation costs ~8–17 s and ~1.4k tokens on this
hardware (measured live runs), and a skipped call cannot hallucinate. It is a deterministic guarantee instead of a probabilistic one.
The integration test asserts `model_calls == 0` and the fake LLM received no requests. The honest
limitation: without a relevance threshold it only fires when retrieval returns nothing (empty
tenant, filters with no matches); the threshold that makes it fire for off-topic questions is
tuned on Day 4.
Pointer: `app/rag/answer.py:Answerer.answer`;
`tests/integration/test_api.py::test_answer_with_no_documents_skips_the_model`.

---

## System design

### Q40. How does the Day 1 RAG pipeline fit into the full AgentOps platform?
**Answer:** It is both the baseline and a building block. `Answerer` is the non-agentic baseline
the custom agent loop and LangGraph workflow are compared against; `HybridRetriever.search` becomes
the `document_search` tool and the `retrieve_evidence` node; `LLMRouter` is the only way any
component calls a model.
Per `docs/architecture/system-design.dot`: API → harness (policy gate, budgets, ledger) → agent
loop or LangGraph (`classify → plan → retrieve → tools → generate → validate_citations → verify →
finalize`). Day 1 already provides `validate_citations` (`validate_draft`), typed transient errors
for the harness retry policy, `ToolCall.raw_arguments` so the harness can reject unparsable tool
args (motivated by the `"17% of 2340"` probe), and the `small` role for classification. Tenant
scoping via `Principal` flows into every tool.
Pointer: `app/services.py:build_services`; `app/llm/schemas.py:ToolCall`; `app/rag/answer.py`.

### Q41. How would the ingestion and retrieval design evolve for production scale?
**Answer:** Durable job queue with workers, blue/green index generations, tenant-partitioned or
externalized vectors, RLS as a second isolation wall, and embedding model as data rather than
schema.
- Queue: `documents` already has `status` and `updated_at`; a job table with `SKIP LOCKED`
  plus a sweeper replaces `BackgroundTasks`.
- Index generations: add `index_generation` to chunks, write new, flip pointer, delete old; fixes
  reindex downtime and data loss on failed reindex.
- Vectors: `Vector(768)` is fixed in model and migration; per-model columns/tables let you migrate
  embedding models incrementally. Partition `document_chunks` by tenant or move to a dedicated
  store when one HNSW no longer fits RAM; only `vector_search.py` changes because callers use
  `Candidate`.
- Isolation: keep `scoped_select` and add Postgres RLS on `tenant_id`.
- Quality: threshold and optional reranker gated by eval results, not intuition.
Pointer: `app/ingestion/pipeline.py`; `app/retrieval/vector_search.py`; `app/db/models.py`.

### Q42. How does the LLM layer support swapping models or providers without touching callers?
**Answer:** Callers ask for a role (`main` or `small`), not a model; `LLMRouter` resolves the role
to an ordered list of `Route(provider, model)`, and providers translate a provider-neutral
`LLMRequest`/`LLMResponse`.
`build_router` wires Ollama (or Fake) as primary and appends the OpenAI-compatible route only
when `fallback_enabled`. Each provider maps transport errors via `_map_http_error` so retry policy
is uniform. Usage, latency, `attempts`, `fallback_used` are uniform fields, so cost/latency
dashboards and the Day 4 model benchmark (qwen3:8b vs 1.7b vs llama fallbacks from product.txt §23)
are config changes. `think=False` by default saves tokens on qwen3.
Pointer: `app/llm/provider.py:LLMRouter`, `Route`; `app/services.py:build_router`;
`app/llm/schemas.py:LLMRequest`.
