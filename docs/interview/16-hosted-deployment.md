# Interview Prep 16 — Hosted Deployment: Vercel UI, Azure Container Apps API, Neon, Groq, In-Process Embeddings

Every answer is grounded in the uncommitted hosted-deployment changes on `main` (2026-11-21/22):
`app/llm/provider.py`, `app/core/{config,retry,errors}.py`, `app/services.py`,
`app/ingestion/embeddings.py`, `app/db/session.py`, `app/main.py`, `app/workspace/jail.py`,
`app/tools/workspace.py`, `web/src/{App.tsx,lib/api.ts}`, `web/vercel.json`,
`deploy/container/Dockerfile`, `scripts/deploy_azure.py`, `tests/unit/test_hosted_deploy.py`.
Format: direct answer, then the reasoning, then a code pointer. Companion:
`docs/journal/16-hosted-deployment.md`. Live numbers come from the build session; the only hosted
eval so far was quota-contaminated and is not a baseline.

---

## Basic

### Q1. What is deployed where?
**Answer:** UI on Vercel (`agentops-ai-demo.vercel.app`, static build of `web/`); API on Azure
Container Apps (consumption plan, 0–1 replicas, 1 vCPU / 2 GiB); image on public GHCR; Postgres +
pgvector on Neon; chat on Groq (`openai/gpt-oss-120b` main, `qwen/qwen3.8-27b` small) with
OpenRouter `google/gemma-4-31b-it:free` as fallback; embeddings in-process with fastembed.
Pointer: `docs/deployment.md`; `deploy/container/Dockerfile` (`ENV` block).

### Q2. What does `llm_provider="openai"` mean?
**Answer:** Any OpenAI-compatible `/chat/completions` API (Groq, OpenRouter, Cerebras…). It needs
`llm_base_url` and `llm_api_key`; `Settings._check` refuses to start without both. `build_router`
then builds an `OpenAICompatibleProvider` as the primary.
Pointer: `app/core/config.py:Settings._check`; `app/services.py:build_router`;
`test_openai_provider_requires_url_and_key`.

### Q3. What is the difference between `json_mode="schema"` and `"object"`?
**Answer:** `schema` sends `response_format: {type: "json_schema", json_schema: {name, schema}}`,
so the provider constrains output to the schema. `object` sends `{type: "json_object"}` (any JSON
object) and appends the schema to the system prompt. Both are validated against the schema by
the caller afterwards.
Pointer: `app/llm/provider.py:OpenAICompatibleProvider.chat`, `_JSON_OBJECT_INSTRUCTION`;
`test_schema_mode_sends_json_schema_response_format`, `test_object_mode_puts_the_schema_in_the_system_prompt`.

### Q4. What is `FastEmbedEmbedder`?
**Answer:** An `Embedder` that runs `nomic-ai/nomic-embed-text-v1.5` on CPU through ONNX
(fastembed), in the API process. Same model and 768 dims as Ollama's `nomic-embed-text`, same
`search_document: `/`search_query: ` prefixes. Lazy load, inference in a thread, one lock.
Pointer: `app/ingestion/embeddings.py:FastEmbedEmbedder`; `pyproject.toml` extra `hosted`.

### Q5. What does `AGENTOPS_WORKSPACE_EXEC_BACKEND=disabled` do?
**Answer:** `Workspace.plan` raises `CommandNotAllowedError` for every command, which blocks
`POST /workspace/exec`, the terminal and the agent tool; and `workspace_tools(exec_enabled=False)`
removes `WorkspaceExec` from the registry so the model is never offered it.
Pointer: `app/workspace/jail.py:Workspace.plan`; `app/tools/workspace.py:workspace_tools`;
`test_disabled_exec_refuses_every_command_and_drops_the_agent_tool`.

### Q6. What do `VITE_API_BASE_URL` and `VITE_DEMO_API_KEY` do?
**Answer:** Build-time variables. `API_BASE` prefixes every request, the SSE stream and its
reconnect, and the terminal WebSocket; empty means same origin. `DEMO_API_KEY` is the fallback
when local storage has no key, so visitors start without pasting one.
Pointer: `web/src/lib/api.ts:API_BASE`, `apiUrl`, `getApiKey`, `openTerminal`; `web/src/vite-env.d.ts`.

### Q7. What is single-origin mode?
**Answer:** With `AGENTOPS_WEB_DIST_DIR` set, `create_app` mounts the built UI with
`StaticFiles(html=True)` at `/`, after all API routers so they win. The hosted image sets it, so
the API URL also serves the UI with no CORS. A missing `index.html` fails at startup.
Pointer: `app/main.py:create_app`; `test_api_serves_the_built_ui_without_shadowing_api_routes`,
`test_missing_ui_build_fails_at_startup`.

### Q8. What does `scripts/deploy_azure.py` do?
**Answer:** Builds the hosted image for linux/amd64 with buildx, pushes it to GHCR tagged with the
git SHA (`-dirty` if uncommitted changes), checks the package is public, creates the resource
group and a Container Apps environment with logs off, then creates or updates the app (0–1
replicas, 1 vCPU / 2 GiB, ingress on 8000), turning secret-looking env keys into Container Apps
secrets.
Pointer: `scripts/deploy_azure.py:main`, `is_secret`, `secret_name`, `ghcr_public`.

### Q9. Why does `config_fingerprint` now include the provider and base URL?
**Answer:** It is part of every answer-cache key. Before, only model names were in it; the same
model name on two vendors (or a different embedding provider) could have served a cached answer
produced elsewhere. Now `llm_provider`, `llm_base_url`, `embedding_provider` and `fastembed_model`
are included.
Pointer: `app/services.py:config_fingerprint`.

---

## Logical

### Q10. Why does a 429 with a short hint wait, but one with a long hint fail at once?
**Answer:** Per-minute buckets refill in seconds, so waiting the hinted time (plus up to 0.5 s
jitter) almost guarantees success, and retrying sooner guarantees failure. A long hint means a
daily bucket is spent; waiting minutes or hours inside an HTTP request is useless, so
`retry_async` re-raises immediately and `LLMRouter.chat` moves to the next route. The cap is
`llm_max_retry_after_s` (20 s).
Pointer: `app/core/retry.py:retry_async`; `test_a_short_provider_hint_is_waited_out`,
`test_a_long_provider_hint_falls_through_at_once`.

### Q11. Why is the request-bucket reset used only when remaining requests are 0?
**Answer:** Groq's 429 reports both buckets. On a tokens-per-minute 429 the request bucket still
has e.g. 895 left but its reset is "2h31m12s" (the daily window). Taking the max (the first
implementation) produced a 9,000 s hint, above the 20 s cap, so the app never waited and burned
through routes. The bucket that refused the call is the one at 0.
Pointer: `app/llm/provider.py:_retry_after`; parametrized case → 7.66 in
`test_retry_after_hint_is_read_from_rate_limit_headers`.

### Q12. Why is a 413 mapped to `RateLimitError`?
**Answer:** On Groq's free tier a single request larger than the per-minute token quota returns
413. That is a quota on this route, not a malformed request: a different model or provider may
accept it. As a transient error it lets the router try the next route; as a plain
`ProviderError` it would have failed the run.
Pointer: `app/llm/provider.py:_map_http_error`; `tests/unit/test_llm.py` (413 case).

### Q13. In what order are routes tried for the `main` role?
**Answer:** `gpt-oss-120b` → `gpt-oss-20b` → `qwen3.8-27b` (Groq alternates) → OpenRouter
`gemma-4-31b-it:free`. Each route gets `retry_async` with the hint logic; a transient failure moves
on, a non-transient one (e.g. 400) raises at once.
Pointer: `app/services.py:build_router`; Dockerfile `AGENTOPS_LLM_MODEL_MAIN_ALTERNATES`;
`test_alternate_models_on_the_primary_come_before_the_fallback_provider`; `LLMRouter.chat`.

### Q14. Why does object mode modify the system message instead of adding a user message?
**Answer:** The schema is an instruction about the output format, which belongs with the system
instructions; appending to the existing system message keeps one system message (some models
handle several poorly). If there is none, one is inserted at index 0. JSON-object mode on some
providers also requires the word "JSON" in the prompt, which the instruction contains.
Pointer: `OpenAICompatibleProvider.chat`; `test_object_mode_adds_a_system_message_when_there_is_none`.

### Q15. Why do the two Postgres drivers need URL rewriting?
**Answer:** Neon gives a libpq URL with `sslmode=require&channel_binding=require`. asyncpg wants
`ssl=` and rejects `channel_binding` (it negotiates SCRAM binding itself); psycopg wants
`sslmode`. `asyncpg_url` and `psycopg_conninfo` derive both from one configured URL, so
migrations, the SQLAlchemy engine and the LangGraph checkpointer all connect.
Pointer: `app/db/session.py`; `app/db/migrations/env.py`; `test_libpq_url_becomes_an_asyncpg_url`.

### Q16. What breaks if the CORS middleware is added inside the routes instead of around the app?
**Answer:** Error responses (401, 429, validation errors) would lack `Access-Control-Allow-Origin`,
so the browser would hide them and the UI would show a generic network error instead of "rate
limited". It is added with `add_middleware`, outside the exception handlers, and exposes
`Retry-After` and `X-Request-ID`.
Pointer: `app/main.py` (comment above `CORSMiddleware`); `test_cors_allows_only_the_configured_ui_origin`.

### Q17. What breaks if the `StaticFiles` mount is moved before the routers?
**Answer:** A mount at `/` matches every path, so it would shadow `/api/v1/*`, `/health` and
`/ready`, returning 404s from the static directory. It is mounted last on purpose.
Pointer: `app/main.py:create_app`; `test_api_serves_the_built_ui_without_shadowing_api_routes`.

### Q18. Why is `WorkspaceExec` removed from the registry instead of just failing?
**Answer:** A tool the model sees but can never use costs a model call, a refused ledger event and
often a confused answer. Removing it changes what the planner can choose. The `plan` check
stays as defence in depth for the API and terminal paths.
Pointer: `app/tools/workspace.py:workspace_tools` (comment).

---

## Advanced

### Q19. How did you know fastembed could replace Ollama without reindexing?
**Answer:** It is the same model (nomic-embed-text v1.5) in ONNX form, same 768 dims and same task
prefixes, and the build session embedded the same texts both ways and got cosine 1.0. So vectors
already in Postgres stay valid. If it had differed, the fingerprint change would only have
invalidated caches; the stored chunks would have needed `agentops reindex`.
Pointer: `FastEmbedEmbedder` docstring; `app/core/config.py` (`fastembed_model` comment);
`test_fastembed_uses_nomic_prefixes_and_loads_once`.

### Q20. Why a lock and a thread around fastembed?
**Answer:** ONNX inference is CPU-bound and synchronous; on the event loop it would freeze every
request, including health checks. `asyncio.to_thread` moves it off the loop. ONNX already uses all
cores, so parallel calls would only contend; one `asyncio.Lock` serialises them.
Pointer: `FastEmbedEmbedder._embed`.

### Q21. What are the failure modes of scale-to-zero here?
**Answer:** ~15 s cold start (container start, migrations, app import; the model is baked in, so no
download), plus Neon's compute resume. In-process state (rate buckets, caches, live SSE runs) is
lost at each scale-down, and a run still streaming when the replica stops is lost. The workspace
disk is ephemeral. The indexing worker polls every 30 min so it does not keep Neon awake; uploads
still index inline.
Pointer: Dockerfile (`AGENTOPS_INDEXING_POLL_S=1800`, model bake); `web/src/App.tsx:Connection`.

### Q22. How does the UI handle a sleeping API?
**Answer:** `Connection.check` polls `/ready` up to 20 times every 3 s (about a minute), shows "API
waking up…" if nothing answers within 2.5 s or the API is not ready, with "The server sleeps when
idle… about 20 seconds". It then checks the key with `whoami`. An `alive()` guard stops it after
unmount so it does not set state on a dead component.
Pointer: `web/src/App.tsx` (`WAKE_ATTEMPTS`, `WAKE_RETRY_MS`, `check`).

### Q23. Why did the GHCR visibility check say "private" for a public image?
**Answer:** The python.org Python build on macOS ships without system root certificates, so
`urlopen` raised an SSL error, which is a `URLError`; the `except` returned `False`, i.e.
"private". The fix uses `ssl.create_default_context(cafile=certifi.where())`. The lesson: a
boolean check whose error path returns the negative answer hides environment failures.
Pointer: `scripts/deploy_azure.py:ghcr_public` docstring.

### Q24. What is the real daily capacity, and what limits it?
**Answer:** Groq's free tier gives each model 200K tokens/day, 8K tokens/min and 1,000
requests/day. One question is about 2,500 tokens (several model calls), so ~80 questions/day on
`gpt-oss-120b`, roughly triple with the alternates (estimate), then the slower OpenRouter free
model. `qwen3.8-27b` is both the small model and a main alternate, so those share a quota. The
evidence cap (16,000 chars) keeps one request under the per-minute token cap.
Pointer: `.env.hosted.example`; `docs/deployment.md` "Capacity"; Dockerfile
`AGENTOPS_AGENT_MAX_EVIDENCE_PASSAGE_CHARS`.

### Q25. What does the 23/38 hosted eval actually tell you?
**Answer:** Very little about quality, a lot about quotas. It ran while Groq's daily token quota
was being exhausted. What held regardless: citation_validity 18/18, source_retrieval 21/21,
structured_output 38/38, unanswerable 5/5, answerable 10/11 (retrieval with fastembed and the
JSON-schema path work). Workspace 0/5 (exec disabled by design, the rest rate-limited) and
adversarial 2/7 (mostly rate-limited) are not quality signals. p50 16.5 s. A clean run after the
quota resets is still pending, and none is saved as a baseline.
Pointer: journal 16 status; `datasets/baselines/` (local only).

---

## Scenario

### Q26. "The demo answers 'rate limited' all afternoon."
**Answer:** Groq's daily quotas are spent. Check: the 429 detail and which route the ledger's
model calls used. Each main model has its own 200K/day; once all three Groq routes are exhausted
the router falls to OpenRouter's free model, which has its own limits. Short term: wait for the
reset, or add another alternate or provider via `AGENTOPS_LLM_MODEL_MAIN_ALTERNATES` /
`AGENTOPS_FALLBACK_*` and `deploy_azure.py --skip-build`. Longer term: paid tier, or lower the
demo tenant's rate. Do not run the full eval on the demo quota.
Pointer: `build_router`; `scripts/deploy_azure.py --skip-build`.

### Q27. "I changed `AGENTOPS_CORS_ORIGINS`, redeployed, and the browser still gets CORS errors."
**Answer:** First check the new revision is serving: Container Apps rolls out a new revision and
the old one answers until then (this happened during the build and resolved by itself). Then
check the origin matches exactly (scheme, host, no trailing slash: `cors_origin_list` strips
it, but a typo in the host is not fixed). Use `curl -i -H "Origin: …" $API/health`.
Pointer: `app/core/config.py:cors_origin_list`; `app/main.py`.

### Q28. "Someone extracted the demo key from the page and is scripting the API."
**Answer:** Expected: the key is public by design. Their damage is bounded by the `demo` tenant:
5 agent runs/min, no admin endpoints, no command execution, its own documents and workspace
only. The real cost is Groq quota. Response: rotate the demo key (redeploy API secrets and the
Vercel env var), lower `AGENTOPS_RATE_LIMIT_AGENT_PER_MIN`, or move to per-visitor short-lived
keys.
Pointer: `web/src/lib/api.ts:DEMO_API_KEY`; Dockerfile `AGENTOPS_RATE_LIMIT_AGENT_PER_MIN=5`.

### Q29. "Groq retires `gpt-oss-120b` tomorrow."
**Answer:** Calls to it return a non-transient 4xx (`ProviderError`), and `LLMRouter.chat` raises
instead of falling through, because only transient errors move to the next route. So it needs a
config change: probe current models for tool calling and JSON-schema output (as was done when
Groq retired the Llama models), set `AGENTOPS_LLM_MODEL_MAIN`, redeploy with `--skip-build`. The
fingerprint changes, so cached answers from the old model are not served.
Pointer: `LLMRouter.chat` (`if not is_transient(exc): raise`); `config_fingerprint`.

### Q30. "At 10x traffic, what breaks first?"
**Answer:** The Groq free quota, long before CPU: 10x of ~80 questions/day is beyond all routes.
Next, max replicas 1 and one ONNX lock serialise embeddings. Then in-process rate limits would be
wrong across replicas. Fix in that order: paid inference, more replicas with Redis-backed limits,
embeddings moved out of the API process.
Pointer: `scripts/deploy_azure.py` (`--max-replicas 1`); `FastEmbedEmbedder._lock`.

---

## Trade-off

### Q31. In-process fastembed vs a hosted embeddings API?
**Answer:** Chosen: in-process. Same vectors as local (no reindex), no vendor, no quota, no data
leaving for embeddings. Cost: image ~920 MB vs ~118 MB and the model in RAM, which excluded
Render's 512 MB tier and forces 2 GiB. A hosted API wins when memory is scarce or traffic is high
enough to want embeddings scaled separately.
Pointer: `FastEmbedEmbedder`; Dockerfile model bake.

### Q32. Vercel front door vs single-origin from the API?
**Answer:** Both are supported. Vercel loads instantly and can explain the cold start; it costs a
CORS configuration and a second deploy target. Single-origin needs no CORS and one deploy, but
the first visitor stares at a blank tab for ~15 s. With `min-replicas 1` single-origin would win.
Pointer: `app/main.py` (CORS and `StaticFiles`); `web/vercel.json`.

### Q33. Same-provider alternates vs going straight to the fallback provider?
**Answer:** Groq meters per model, so another Groq model is fresh quota on a fast provider with
the same JSON-schema support. The free OpenRouter model is slower and needs object mode. The cost
is a longer chain on total exhaustion (each route gets its retries), and `fallback_used` no longer
means "other vendor".
Pointer: `build_router`; `LLMRouter.chat` (`fallback_used = index > 0`).

### Q34. Azure Container Apps vs HF Spaces vs Render vs a VM?
**Answer:** HF Docker Spaces needs PRO ($9/mo); Render's free 512 MB cannot hold the model; the
Oracle free VM failed for the user and would need patching and TLS by hand. Container Apps gives
HTTPS ingress, a secret store, revisions and scale-to-zero within a free monthly grant (~50
active hours at this size). It costs a cold start and needs a budget alert as a guard.
Pointer: `docs/deployment.md`; `scripts/deploy_azure.py`.

---

## Justification

### Q35. "Shipping an API key in the JavaScript bundle is a security hole."
**Answer:** It is a deliberate public credential for a demo tenant, not a secret: it can only do
what an anonymous visitor should be able to do, at 5 agent runs a minute, in a tenant with no
admin rights and no command execution. The admin key is separate and never in the bundle. The
alternative (making visitors paste a key) kills the demo. The honest risk is quota exhaustion, not
data exposure.
Pointer: `web/src/lib/api.ts` (`DEMO_API_KEY` comment); `.env.hosted.example`.

### Q36. "Why not just allow `*` for CORS? It's a public demo."
**Answer:** To be precise about the threat: the key is sent in a header, not a cookie, so the
browser never attaches it automatically, and another origin cannot read this UI's local storage.
`*` would not leak a stored key by itself. What the allow-list buys: other sites cannot run a browser
front end against this API (for example a look-alike page that asks users to paste their key);
it does not stop a server or script from using the public demo key, which only the rate limit
bounds. It also stays safe if cookie or session auth is ever added (where `*` would be
dangerous). An explicit list costs one env var; the validator makes `*` a startup error so nobody
"fixes" a CORS error by widening it.
Pointer: `app/core/config.py:_check`; `test_wildcard_cors_is_refused`.

### Q37. "Disabling command execution guts a headline feature."
**Answer:** The feature's safety depended on the Docker sandbox, which Container Apps cannot
run. The `host` backend would execute approved code in the container that holds the database URL
and API keys. A disabled feature is honest; an unsandboxed one on the public internet is not. It
stays available locally with the Docker backend, and the workspace eval cases document it.
Pointer: `app/core/config.py` (`workspace_exec_backend` comment); `docs/deployment.md`.

---

## System design

### Q38. Draw the hosted request path for one question.
**Answer:** Browser → Vercel static page → `POST https://<api>/api/v1/agent/run/stream` with
`X-API-Key` (CORS preflight against the allow-list) → Container Apps ingress (wakes the replica if
at zero) → FastAPI auth and rate limit → agent graph: small model (Groq) classifies, query
embedded in-process (fastembed), pgvector search on Neon, main model (Groq) answers with schema
output, verifier checks → SSE events back to the browser; ledger and execution saved in Neon.
Each model call walks the route list on a 429/413/5xx.
Pointer: `docs/deployment.md` diagram; `app/services.py:build_services`.

### Q39. How would this evolve from a free demo to a small production service?
**Answer:** Paid inference with prices configured (so `max_cost_usd` works) and a quota-aware
router; `min-replicas 1` and a log sink; Redis for rate limits and caches with 2+ replicas; a
sandbox service to re-enable exec; per-user auth instead of a shared demo key; CI deploying
tagged commits (today the image can be `-dirty`); a clean hosted eval baseline wired into drift.
Pointer: journal 16 §8-§9.

### Q40. Summarise this milestone in one minute.
**Answer:** I made the same codebase run with no model servers of its own and $0 a month. Chat
goes to any OpenAI-compatible API (Groq, with same-provider alternates and an OpenRouter
fallback); embeddings run in-process with the ONNX build of the same nomic model, so vectors match
local exactly. The API runs on Azure Container Apps at scale-to-zero, Postgres on Neon, the UI on
Vercel with a "waking up" state. Live testing found real bugs: 429 hints read from the wrong
bucket so we never waited, daily per-model quotas fixed with alternates, a deploy check that read
an SSL failure as "private". Security: explicit CORS, a public rate-limited demo tenant, secrets
in the platform store, and command execution disabled because there is no sandbox. The live
agent answers a cited, verified question in 17–20 s. Not done: a clean hosted eval baseline
(the only run, 23/38, was quota-contaminated), and the changes are not yet committed.
Pointer: journal 16.
