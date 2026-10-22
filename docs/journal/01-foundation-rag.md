# Journal 01 — Foundation + LLM + RAG (Day 1)

Milestone: Day 1 of product.txt §18. Status: implemented, 104 tests green, 3 live Ollama tests green,
ruff + mypy `--strict` clean. Written by the Recorder agent from the code, not from the plan.

---

## 1. What was built, by subsystem

### Core (`app/core/`)
- **Settings** — `config.py:Settings`. Pydantic settings with prefix `AGENTOPS_`. Validator `_check`
  rejects `chunk_overlap_chars >= chunk_size_chars`, and in `env=prod` refuses to start without
  `AGENTOPS_API_KEYS` or with the dev `DATABASE_URL`. `fallback_enabled` is true only when all three
  fallback settings are present.
- **Errors** — `errors.py`. One `AppError` hierarchy; each class carries `code`, `status_code`,
  `transient`. `message` is client-safe, `detail` is log-only. Transient: `ProviderUnavailableError`
  (503), `ProviderTimeoutError` (504), `RateLimitError` (429). Everything else is permanent.
- **Retry** — `retry.py:retry_async`. At most `max_retries + 1` attempts, full-jitter backoff
  (`backoff_delay` = `uniform(0, min(max, base·2^attempt))`). Retries only if `is_transient(exc)`.
- **Logging** — `logging.py`. JSON lines with `request_id` and `tenant_id` from ContextVars;
  `redact` masks sensitive keys (api_key, authorization, password, secret, token, cookie), bearer
  tokens, `sk-…` keys and DSN passwords, recursively.
- **Security** — `security.py:ApiKeyAuthenticator`. Keys parsed from `key:tenant,...`
  (`parse_api_keys`, min 16 chars). Stored as SHA-256 digests; `authenticate` compares against
  every digest with `hmac.compare_digest` (no early exit). `Principal.key_id` is a 12-hex
  fingerprint, safe to log. Dev keys (`dev-key-tenant-a/b`) only when no keys are set and env≠prod.

### Database (`app/db/`)
- `models.py`: `Document` (tenant, filename, sha256 `content_hash`, `version`, `is_latest`,
  `status` enum pending/processing/ready/failed, `metadata` JSONB, deferred `extracted_text`) and
  `DocumentChunk` (deterministic UUID PK, denormalized `tenant_id`, char/page offsets, `section`,
  `Vector(768)` embedding, generated `tsv` = `to_tsvector('english', section || ' ' || text)`).
- Constraints: `uq_documents_tenant_hash` (idempotency), `uq_documents_tenant_file_ver`
  (versioning), `uq_chunks_doc_index`. Indexes: HNSW cosine (`m=16, ef_construction=64`), GIN on
  `tsv`, `(tenant_id, document_id)`, `(tenant_id, created_at)`.
- `migrations/versions/0001_documents_and_chunks.py` creates all of it plus `CREATE EXTENSION
  vector`; downgrade drops tables and the enum type.

### Ingestion (`app/ingestion/`)
- `parsers.py:extract_text` — extension allowlist (`.txt .md .markdown .pdf`), PDF must start with
  `%PDF-`, encrypted PDFs rejected, text files must have no NUL bytes and be UTF-8 (BOM tolerated).
  `normalize_text`: NFKC (fixes ligatures), CRLF→LF, strips control chars and zero-width chars
  (U+200B, U+2060, U+FEFF), collapses 3+ blank lines; PDF pages joined with `\f`. Empty result →
  `EmptyDocumentError` (with an OCR hint for PDFs). `sanitize_filename` strips paths and control
  chars.
- `chunking.py` — `structured` (default) and `fixed` strategies, both returning exact character
  spans into the normalized text. `stable_chunk_id` = uuid5(document_id, config key, index, text
  hash).
- `embeddings.py` — `OllamaEmbedder` (batched `/api/embed`, `truncate: true`, retries, nomic
  `search_document:` / `search_query:` prefixes, `_validate` checks count, dimension, NaN/inf) and
  `FakeEmbedder` (hashed bag-of-words, deterministic, for tests).
- `pipeline.py:IngestionService` — `create_document` (request path) and `process_document`
  (background path), plus tenant-scoped `get/list/delete/mark_for_reindex`.
  `embedding_input` prepends `filename — section` to each chunk before embedding.

### Retrieval (`app/retrieval/`)
- `vector_search.py:scoped_select` — the single base query for every retriever: joins chunk →
  document and filters `chunk.tenant_id`, `document.tenant_id`, `status=READY`, `is_latest=true`,
  plus optional `document_ids` and JSONB `metadata @>` filters.
- `vector_search.py:vector_search` — cosine distance ordered, with `SET LOCAL
  hnsw.iterative_scan = strict_order` and `hnsw.ef_search = max(40, min(limit*2, 1000))`.
- `keyword_search.py:keyword_search` — `websearch_to_tsquery`, text-rewritten `' & '`→`' | '`, ranked
  by `ts_rank_cd(..., 32)`.
- `hybrid_search.py` — `reciprocal_rank_fusion` (k=60), `dedupe` by `text_hash`, and
  `HybridRetriever.search` with per-stage timings and candidate counts in `RetrievalDiagnostics`.
  Hybrid fetches `max(top_k, candidate_k=30)` per retriever.

### LLM (`app/llm/`)
- `schemas.py` — provider-neutral `Message`, `ToolCall` (with `raw_arguments` when args are not a
  JSON object), `ToolSpec`, `Usage`, `LLMRequest` (`json_schema`, `think=False`,
  `temperature=0.0`), `LLMResponse` (usage, latency, `attempts`, `fallback_used`).
- `provider.py` — `OllamaProvider` (native `/api/chat`, `format`=JSON schema, tools, `think`),
  `OpenAICompatibleProvider` (`/chat/completions`, `response_format: json_schema`),
  `FakeProvider`, `_map_http_error` (timeout/transport/5xx → transient; 429 → `RateLimitError`;
  other 4xx → permanent `ProviderError`), and `LLMRouter` (role `main|small` → ordered routes,
  retry per route, fall through to next route only on transient errors).
- `usage.py:estimate_cost` — ollama/fake → `$0`, `pricing_source="local"`; hosted model not in
  `HOSTED_PRICES` → `None`, `"unavailable"`. `HOSTED_PRICES` is empty on purpose.

### RAG (`app/rag/answer.py`)
`Answerer.answer`: retrieve → `select_evidence` (char budget 8000, always ≥1 chunk) → if no
evidence, return `insufficient_evidence` with **zero model calls** → `build_messages` (evidence in
`<source id="S1" ...>` fences, `_escape` neutralises `</source>`/`<evidence>` breakouts, system
prompt says evidence is untrusted) → `main` model with `AnswerDraft` JSON schema →
`validate_draft` (schema, unknown IDs in `citations` or inline `[Sn]`, uncited claims, empty
answer) → at most one correction turn listing valid IDs → `GroundedAnswer` with status
`answered | insufficient_evidence | failed_validation`, citations mapped back to chunk/doc/page,
usage, cost, `prompt_version="rag-answer-v1"`, timings.

### API, composition, CLI
- `app/services.py:build_services` — composition root; tests inject fakes into the same container.
- `app/main.py:create_app` — factory, lifespan, `request_context` middleware (validates incoming
  `X-Request-ID` against `^[A-Za-z0-9._-]{1,64}$`, else generates one; access log with tenant
  from `request.state`), uniform error envelope `{"error": {code, message, request_id}}`,
  validation errors return field locations only.
- `app/api/deps.py:get_principal` — authenticates, sets `request.state.tenant_id`.
- `app/api/documents.py` (upload 202/200, list, get, delete 204, reindex 202), `retrieval.py`
  (`/retrieval/search`, `/rag/answer`), `llm.py` (`/llm/chat`, `/models`), `health.py`
  (`/health`, `/ready` checks DB, Ollama and that configured models are installed).
- `app/cli.py` — `agentops ingest | search | ask`, synchronous, no HTTP server needed.

### Infra and tests
- `docker-compose.yml`: `pgvector/pgvector:0.8.1-pg17` on host port 5433.
- `tests/conftest.py`: creates `agentops_test`, runs Alembic `downgrade base` then `upgrade head`
  every session; `AGENTOPS_REQUIRE_DB=1` turns "DB missing" from skip into failure.
- `tests/unit/` (parsers, chunking, core, llm, retrieval+answer), `tests/integration/` (api,
  ingestion, retrieval on the 4-doc corpus in `datasets/corpus/`), `tests/live/` (opt-in).
- `.github/workflows/ci.yml` written, not yet run on GitHub (no remote).

---

## 2. Files touched

```
app/core/{config,errors,logging,retry,security}.py
app/db/{models,session}.py, app/db/migrations/env.py, app/db/migrations/versions/0001_documents_and_chunks.py
app/ingestion/{parsers,chunking,embeddings,pipeline}.py
app/retrieval/{vector_search,keyword_search,hybrid_search,schemas}.py
app/llm/{schemas,provider,usage}.py
app/rag/answer.py
app/services.py, app/main.py, app/cli.py
app/api/{deps,documents,retrieval,llm,health}.py
tests/conftest.py, tests/unit/*, tests/integration/*, tests/live/test_ollama_live.py
docker-compose.yml, pyproject.toml, uv.lock, alembic.ini, .github/workflows/ci.yml
datasets/corpus/*.md, README.md, docs/day-checklist.md, docs/architecture/system-design.{dot,png}
```

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **RRF** on ranks (`hybrid_search.py:reciprocal_rank_fusion`, k=60) | Weighted blend of cosine + `ts_rank_cd` | The two scores live on incomparable scales; blending needs a tuned weight and normalization per corpus. RRF needs no tuning and rewards agreement. Cost: loses score magnitude ("how much better"), so it cannot express "vector is very confident, keyword is noise". |
| 2 | **pgvector** inside Postgres | Dedicated vector DB (Qdrant, Weaviate, Pinecone) | One system, one transaction for chunks + status (decision 4), SQL joins for tenant/status/metadata filters, FTS in the same query engine. Cost: HNSW filtered search is post-filter (mitigated by decision 13); scale ceiling is lower than a dedicated store. |
| 3 | **Two-phase ingestion**: validate+extract+store in the request, chunk+embed in background (`pipeline.py`) | Fully synchronous upload, or accept bytes and validate later | Bad files fail fast with 4xx and are never stored; slow embedding does not hold the request. Cost: background is in-process `BackgroundTasks` (crash → stuck `processing`). |
| 4 | **Atomic READY**: delete old chunks, insert new, demote older versions, flip status — one transaction (`process_document`) | Insert chunks incrementally and mark READY at the end | No partially-searchable document can exist. On any failure: FAILED, 0 chunks (`test_embedding_failure_never_marks_ready`). Cost: whole-document embeddings held in memory; a failed reindex removes the previously good chunks (fail-closed). |
| 5 | **404 for cross-tenant** (`IngestionService.get`, `delete`) | 403 Forbidden | 403 confirms the ID exists for someone else (existence oracle). 404 is identical for "missing" and "not yours". Cost: harder support debugging; mitigated by request IDs in logs. |
| 6 | **Hashed API keys**, compare against all digests (`security.py`) | Plain dict lookup of raw keys | Heap dumps/debug prints do not reveal keys; no early exit. Cost: O(n keys) per request — fine for a handful of keys, not for thousands (would move to a DB lookup by key prefix). |
| 7 | **Fallback only on transient errors** (`LLMRouter.chat`) | Fall back on any error, or on bad output | A 4xx or bad model output is a request/quality problem; another provider will not fix it and it would hide bugs and leak data to a hosted model unnecessarily. |
| 8 | **No-evidence short-circuit** (`Answerer.answer`, `if evidence:`) | Always call the model | No tokens spent, nothing to hallucinate from (`test_answer_with_no_documents_skips_the_model`). Caveat: without a relevance threshold, vector search returns neighbours whenever a tenant has any READY doc, so this only fires for empty tenants / filters that match nothing. Out-of-scope questions still reach the model, which then has to say `insufficient_evidence`. |
| 9 | **Structured chunking** default, `fixed` kept as baseline (`chunking.py`) | Fixed windows only, or semantic/LLM chunking | Paragraph/heading aware chunks keep sections intact and labelled; `fixed` exists so the gain is measured (Day 4 eval), not assumed. LLM chunking rejected: slow on a 16 GB laptop, non-deterministic. |
| 10 | **Store `extracted_text`** (deferred column) | Re-parse the original upload, or store the raw bytes | Reindex (new chunk size, new embedding model) needs only the DB; normalized text also defines the char offsets citations point to. Cost: storage roughly equal to text size; raw file not kept. |
| 11 | **Deterministic chunk IDs** (`stable_chunk_id`) | Random UUIDs | Same doc+config+content → same ID, so citations and future eval labels survive a re-run; a different config gives different IDs (`test_chunk_ids_are_stable_and_config_sensitive`). |
| 12 | **nomic task prefixes** (`OllamaEmbedder`) | Embed queries and docs identically | nomic-embed-text is trained with `search_document:`/`search_query:`; omitting them degrades retrieval. Prefixes applied only if the model name contains `nomic`. |
| 13 | **Iterative HNSW scan** (`vector_search`, `SET LOCAL`) | Default HNSW, or partition per tenant | HNSW filters after the scan; a selective tenant filter could return fewer than `limit` rows. `strict_order` keeps scanning; `SET LOCAL` scopes the setting to the transaction. Partitioning rejected as premature. |
| 14 | **Keyword OR-rewrite** (`keyword_search`) | Keep `websearch_to_tsquery` AND semantics, or `plainto_tsquery` | AND made natural questions match nothing (bug 2). OR favours recall; `ts_rank_cd` still ranks multi-term matches higher; RRF then filters noise. |
| 15 | **Idempotency on (tenant, sha256)** + versioning by filename | Dedupe by filename only | Re-uploads are free and safe; new content under the same name becomes v+1 and replaces the old version in search only once READY. |

---

## 4. Commands

```bash
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run agentops ingest datasets/corpus
uv run agentops search "how long are transaction records kept"
uv run agentops ask "What is the RPO in v2 and how does it differ from v1?"
uv run uvicorn --factory app.main:create_app --port 8000      # docs at /docs

uv run pytest                 # unit + integration (real Postgres, fake LLM/embedder)
uv run pytest -m live         # opt-in, real Ollama
uv run ruff check app tests && uv run ruff format --check app tests && uv run mypy app
```

---

## 5. Results (measured on Apple M4, 16 GB, Ollama 0.40.7)

- Tests: **104 passed** (unit + integration on Postgres 17 / pgvector 0.8.1, Alembic
  downgrade→upgrade each run); **3 live** Ollama tests passed; ruff + mypy `--strict` clean.
- Models: qwen3:8b ≈ 15.7 tok/s (5.6 GB, 100% GPU); qwen3:1.7b ≈ 52.5 tok/s; nomic-embed-text
  768-dim.
- Ingestion of the 4-doc corpus: ~1.7 s. Re-ingest: all reported as duplicates.
- "RPO in v2 vs v1": correct, cited both docs, 1318 input + 91 output tokens, ~17 s generation.
  (v1 and v2 are different filenames in `datasets/corpus/`, so both are `is_latest`.)
- Escalation question (runbook): correct, ~8 s.
- "CEO's favorite programming language": `insufficient_evidence`, no hallucination. Note the model
  was called (evidence was non-empty, see decision 8); the model made the call, not the
  short-circuit.
- PDF upload: answer cited the page. Tenant B search on tenant A's content: 0 results.
- Early probes: qwen3:8b emitted a `calculator` call with `"17% of 2340"` (not a valid
  expression) → tool args must be validated by the harness (Day 2). On one classification probe
  1.7b beat 8b — a single sample, not a measurement.

---

## 6. Bugs found and fixed

1. **Zero-width-only file accepted as non-empty.** Root cause: `str.strip()` does not remove
   U+200B/U+2060/U+FEFF. Fix: `parsers.py:_INVISIBLE` stripped in `normalize_text`. Also closes a
   way to hide injected instructions in documents. Guard: `tests/unit/test_parsers.py::test_empty_documents`
   (parametrized with `"​"`).
2. **Keyword search returned 0 candidates for questions.** Detected from live diagnostics
   (`keyword_candidates: 0`). Root cause: `websearch_to_tsquery` ANDs every term, so "how long are
   records kept" required every stem in one chunk. Fix: cast to text, replace `' & '` with
   `' | '`, cast back. Hybrid then surfaced chunks found by both retrievers. Guard:
   `tests/integration/test_retrieval.py::test_keyword_search_matches_natural_language_questions`.
3. **Reindexing an old version could re-promote it.** Root cause: the promotion step in `process_document`
   (demote siblings, mark self latest) did not consider whether a newer version existed. Fix: count newer READY versions of the same filename;
   only demote siblings / set latest if none. Guard: second half of
   `tests/integration/test_ingestion.py::test_new_content_same_filename_creates_new_version`.
4. **Access log had `tenant_id: null`.** Root cause: a ContextVar set during dependency resolution
   does not propagate back to the middleware's context. Fix: `get_principal` also writes
   `request.state.tenant_id`; the middleware reads that. Guard:
   `tests/integration/test_api.py::test_access_log_includes_tenant`.
5. **Multi-section chunks labelled with the first heading only.** Fix: `_structured.flush` joins
   distinct sections of the non-overlap units with `" | "`. Guard:
   `tests/unit/test_chunking.py::test_chunk_spanning_sections_lists_all_of_them`.

---

## 7. Known limitations and open findings

Planned / acknowledged:
- In-process `BackgroundTasks`: a crash mid-index leaves the doc `processing` forever; no sweeper.
- No vector relevance threshold (Day 4, tuned on the eval set).
- No OCR: text-less PDFs are rejected with a hint.
- Embedding dimension fixed in schema (`Vector(768)` in model and migration 0001).
- No rate limiting yet (Day 4).
- Hosted fallback only mock-tested; `HOSTED_PRICES` is empty.

Found by the Recorder while reading the code. The first two were then reproduced with failing
tests and **fixed the same day**; the rest remain open:
- **FIXED — Tenant ContextVar inside endpoints was null.** `deps.py:get_principal` was a sync
  `def`, so FastAPI ran it in a threadpool copy of the context and `tenant_id_var.set` never
  reached the endpoint coroutine; log lines inside handlers carried `tenant_id: null`. Fix:
  `get_principal` is now `async def`. Guard: `tests/integration/test_api.py::
  test_logs_inside_endpoints_carry_tenant` (verified to fail with the sync version).
- **FIXED — Negation inverted after the OR rewrite.** `failover -patroni` became
  `'failov' | !'patroni'`, matching nearly every chunk. Fix:
  `keyword_search.py:split_negations` pulls `-term`/`-"phrase"` out before parsing and applies them
  as a separate `NOT tsv @@ …` filter. Live: `failover -patronictl` now returns 2 candidates.
  Guards: `test_negated_terms_exclude_rather_than_match_everything`, `test_split_negations`.
- **Reindex takes the document out of search** while `pending/processing` (`scoped_select` needs
  READY), and a **failed reindex deletes the previous good chunks**; an older version is not
  re-promoted. Same for **deleting the latest version**: earlier versions stay `is_latest=false`.
- **Re-uploading old content is a no-op**: uploading v1's bytes after v2 returns the v1 document
  as a duplicate and does not make it latest. Re-uploading bytes of a FAILED doc also returns the
  failed doc; the client must call reindex.
- `mark_for_reindex` is check-then-update (not atomic); two concurrent calls can both schedule.
- Citation validation checks that cited IDs exist, not that the cited text supports the claim
  (entailment check is the Day 2 `verify_answer` node).

---

## 8. What I'd do differently at 100x scale

- **Durable queue** (Postgres `SELECT … FOR UPDATE SKIP LOCKED`, or Redis/SQS) with a lease and a
  sweeper for stuck `processing` docs; separate embedding workers sized to GPU throughput.
- **Blue/green reindex**: write new chunks under a new `index_generation`, flip a pointer
  atomically, then delete old — no search gap and no data loss on a failed reindex.
- **Embedding model as data, not schema**: per-model chunk tables or `vector` without fixed dim
  plus partial HNSW indexes per model, so models can be migrated incrementally.
- **Tenant-aware indexing**: partition `document_chunks` by tenant (or tenant tier), partial HNSW
  indexes for large tenants; or move vectors to a dedicated store when a single HNSW exceeds RAM.
- **Postgres row-level security** keyed on a session tenant setting as a second wall behind
  `scoped_select`.
- **API keys in a table** (prefix + hash, rotation, revocation, per-key scopes and rate limits)
  instead of an env var.
- **Relevance threshold + reranker** decided by the eval set; caching query embeddings.
- **Streaming and token budgets** per tenant; hosted fallback with a verified price table and an
  explicit data-egress policy per tenant.
