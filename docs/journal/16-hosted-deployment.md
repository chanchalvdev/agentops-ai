# Journal 16 — Hosted Deployment: Vercel UI, Azure Container Apps API, Neon, Groq, In-Process Embeddings

Milestone: a free hosted deployment of the whole system (Sat 21 – Sun 22 Nov), now live:
- UI: **https://agentops-ai-demo.vercel.app** (Vercel, static build of `web/`);
- API: `https://agentops-api.bluecoast-6af9a19c.centralindia.azurecontainerapps.io` (Azure
  Container Apps, consumption plan, 0–1 replicas, 1 vCPU / 2 GiB, Central India, logs off);
- image: public GHCR package `ghcr.io/<github-user>/agentops-ai:<sha>`, built for linux/amd64 on
  an ARM Mac with `docker buildx`;
- database: Neon Postgres with pgvector;
- chat: Groq `openai/gpt-oss-120b` (main, `reasoning_effort=low`) and `qwen/qwen3.8-27b` (small),
  then other Groq models, then OpenRouter `google/gemma-4-31b-it:free`;
- embeddings: fastembed `nomic-ai/nomic-embed-text-v1.5`, in-process, baked into the image.

The changes are **uncommitted** on `main` (26 modified files, +941/−84, plus new
`deploy/container/Dockerfile`, `scripts/deploy_azure.py`, `tests/unit/test_hosted_deploy.py`,
`docs/deployment.md`, `.env.hosted.example`, `web/vercel.json`, `web/src/vite-env.d.ts`). Written
by the Recorder from `git diff`, the new files and the tests. Live results (latencies, eval scores,
the hosting attempts that failed) come from the build session and are marked as such; no hosted
eval report is saved under `datasets/baselines/` or `reports/`.

Status (re-run by the Recorder on the working tree):
- `uv run pytest -q -W error tests/unit tests/security` → **341 passed** (was 308; the 31 tests in
  `tests/unit/test_hosted_deploy.py` plus one new 413 case in `tests/unit/test_llm.py`).
- `npx vitest run` in `web/` → **13 passed** (two new API base URL tests).
- `ruff check app tests scripts` clean; `mypy app` (strict) → no issues in 100 source files.
- Integration tests were not re-run for this entry.
- CI gains a step that builds `deploy/container/Dockerfile` and imports `fastembed` and `app.main`
  inside it (`.github/workflows/ci.yml`).

Live results (build session):
- The live agent answered the RPO question ("RPO 5 minutes", citing
  `payments-architecture-v2.md`, verified) in 17–20 s warm; a cold start adds about 15 s.
- One hosted evaluation run, **contaminated by Groq quota exhaustion**: 23/38 overall.
  answerable 10/11, unanswerable 5/5, citation_validity 18/18, source_retrieval 21/21,
  structured_output 38/38; workspace 0/5 (exec disabled by design, the rest rate-limited);
  adversarial 2/7 (mostly rate-limited); p50 16.5 s. This is **not** a baseline: a clean hosted
  run is pending until the daily quota resets. The committed baselines are still local qwen3.

---

## 1. How the host was chosen

Constraint: $0 a month, on hardware the user does not own, for a demo that may sit idle for
days. In order of attempts (build session):

| Option | Outcome |
|---|---|
| Host Ollama models too | Skipped: a GPU or a large-RAM VM is not free, and qwen3:8b on shared CPU would be minutes per answer. Chat moves to a hosted OpenAI-compatible API. |
| Oracle Cloud Always Free (ARM VM) | Failed for the user (account/capacity); abandoned. |
| Hugging Face Docker Spaces | First plan (see the memory note "Hosted deploy plan"). Docker Spaces turned out to need PRO ($9/month), so the plan was reversed. |
| Render free web service | 512 MB RAM is too small: the image is ~920 MB with fastembed and its ONNX model vs ~118 MB without, and the model must sit in memory. |
| Azure $200 trial credit | Not needed: the Container Apps monthly free grant (180,000 vCPU-s, 360,000 GiB-s, 2M requests) covers about 50 active hours a month at 1 vCPU / 2 GiB, and idle time costs nothing with 0 replicas. |
| **Azure Container Apps + Vercel + Neon + Groq** | Chosen. |

Why the UI is on Vercel although the API image can serve it (`AGENTOPS_WEB_DIST_DIR`, §2.6): a
scale-to-zero API takes ~15 s to answer the first request. A static page on Vercel loads at
once and shows "waking up…" while it polls `/ready`, instead of a blank tab for 15 s. The name
`agentops-ai.vercel.app` was taken, so the project lives at `agentops-ai-demo.vercel.app` (the
example in the `cors_origins` comment in `app/core/config.py` still says `agentops-ai`).

---

## 2. What was built, by subsystem

### 2.1 Hosted chat provider (`app/llm/provider.py`, `app/core/config.py`, `app/services.py`)

- **`llm_provider="openai"`**: any `/v1/chat/completions` API. Needs `llm_base_url` and
  `llm_api_key`; `Settings._check` refuses to start without them.
- **`OpenAICompatibleProvider`** gained keyword arguments `name`, `json_mode`,
  `reasoning_effort`. `provider_label(base_url)` derives a vendor name from the host
  (`api.groq.com` → `groq`, `openrouter.ai` → `openrouter`) for traces and cost reports.
- **Two structured-output modes.** `json_mode="schema"` sends `response_format: {type:
  json_schema, json_schema: {name: "output", schema}}`. `json_mode="object"` sends `{type:
  json_object}` and appends `_JSON_OBJECT_INSTRUCTION` + the schema to the system message (or
  inserts a system message). JSON mode only guarantees *some* object, and some providers require
  the word "JSON" in the prompt. The caller validates the output against the schema in both modes.
  Groq's gpt-oss and qwen models use `schema`; the OpenRouter fallback uses `object`
  (`fallback_json_mode`, default `"object"`).
- **`reasoning_effort`** is passed through when set (`AGENTOPS_LLM_REASONING_EFFORT=low` in the
  image) so gpt-oss does not spend its output budget thinking.
- **Alternates.** `llm_model_main_alternates` / `llm_model_small_alternates` (comma-separated).
  `build_router` builds each role's route list as: primary model → alternates on the same
  provider → fallback provider. The image sets main = `gpt-oss-120b, gpt-oss-20b, qwen3.8-27b`,
  small = `qwen3.8-27b, gpt-oss-20b`, then OpenRouter (`fallback_model_small` defaults to
  `fallback_model`).
- **`config_fingerprint`** now includes `llm_provider`, `llm_base_url`, `embedding_provider` and
  `fastembed_model`, so switching vendor never serves answers cached from another one.
- `GET /api/v1/models` (`app/api/llm.py`) only queries Ollama's `/api/tags` when Ollama is
  actually a provider; hosted and in-process models report `installed=true` ("available here").

### 2.2 Rate limits: wait out short hints, fall through on long ones (`app/core/retry.py`, `app/core/errors.py`, `app/llm/provider.py`)

- `RateLimitError` carries `retry_after_s`, the provider's own "try again in" hint.
- **`_map_http_error`**: 429 → `RateLimitError(retry_after_s=_retry_after(headers))`; **413** →
  `RateLimitError` too. Groq returns 413 when one request exceeds the per-minute token quota:
  a quota, not a malformed request, so another route may accept it. Both are transient.
- **`_retry_after(headers)`**: `Retry-After` (seconds) first; then Groq/OpenAI reset headers
  parsed by `_duration_s` (`"577ms"`, `"7.66s"`, `"1m26.4s"`, `"2h31m12s"`). The request-bucket
  reset (`x-ratelimit-reset-requests`) counts **only when** `x-ratelimit-remaining-requests` is
  `"0"`; otherwise the token reset is used (bug 2 in §4).
- **`retry_async(..., max_retry_after_s=)`**: with a hint ≤ the cap, sleep `hint + U(0, 0.5)`
  instead of the jittered backoff (retrying sooner is certain to fail); with a hint above the
  cap, re-raise at once so `LLMRouter.chat` moves to the next route. No hint → the old backoff.
  `AGENTOPS_LLM_MAX_RETRY_AFTER_S` defaults to 20 (bounded 0–60; 0 disables it); the image sets
  `AGENTOPS_LLM_MAX_RETRIES=3`.
- `LLMRouter.chat` was not restructured: it already caught transient errors per route and tried
  the next one. Note `fallback_used` is `index > 0`, so a same-provider alternate is reported as
  a fallback too.

### 2.3 In-process embeddings (`app/ingestion/embeddings.py`, `pyproject.toml`)

- **`FastEmbedEmbedder`**: ONNX on CPU via fastembed, installed with the new optional extra
  `hosted = ["fastembed>=0.9.1"]` (`uv sync --extra hosted`). Default model
  `nomic-ai/nomic-embed-text-v1.5`, the model Ollama serves as `nomic-embed-text`, so the 768-dim
  schema and the `search_document: ` / `search_query: ` prefixes stay. The build session measured
  cosine 1.0 against Ollama's vectors for the same text, so the existing local index and the
  hosted one are interchangeable and no migration or reindex was needed for the switch.
- The model loads lazily (`_factory`, injectable as `model_factory` for tests), inference runs in
  `asyncio.to_thread`, and one `asyncio.Lock` serialises calls (ONNX already uses every core).
  Failures become `EmbeddingError`; `_validate` still checks count and dimension.
- `build_embedder(settings, http)` in `app/services.py` replaces the inline choice; the image sets
  `AGENTOPS_FASTEMBED_CACHE_DIR=/opt/models` and downloads the model at build time, so a cold
  start never fetches it.

### 2.4 Neon URLs for two drivers (`app/db/session.py`, `app/db/migrations/env.py`)

Neon hands out libpq URLs (`postgresql://…?sslmode=require&channel_binding=require`). SQLAlchemy
uses asyncpg, the LangGraph checkpointer uses psycopg 3, and they spell TLS differently.
- `asyncpg_url(url)`: scheme `postgresql+asyncpg`, `sslmode` → `ssl`, `channel_binding` dropped
  (asyncpg negotiates SCRAM channel binding itself).
- `psycopg_conninfo(url)`: scheme `postgresql`, `ssl` → `sslmode`.
- `make_engine`, Alembic's `run_async_migrations` and `Services`' checkpointer pool use them, so
  the operator pastes Neon's URL unchanged. The old `.replace("postgresql+asyncpg://", …)` is gone.

### 2.5 Command execution disabled when hosted (`app/workspace/jail.py`, `app/tools/workspace.py`, `app/core/config.py`)

Container Apps cannot run the Docker exec sandbox (no Docker daemon, no privileged containers),
and the `host` backend would run approved code with the API's privileges and secrets.
- `workspace_exec_backend` gains `"disabled"`. `WorkspaceManager(exec_enabled=...)`;
  `Workspace.plan` raises `CommandNotAllowedError("Command execution is disabled on this
  deployment")` first, which covers `POST /workspace/exec`, the terminal WebSocket and the agent
  tool, all of which plan before running.
- `workspace_tools(..., exec_enabled=False)` leaves `WorkspaceExec` out of the registry, so the
  model is never offered a tool that can only fail. Read/write/edit tools still work; the disk is
  ephemeral (`/data/workspace`).

### 2.6 One container, two front doors (`app/main.py`)

- **CORS allow-list.** `AGENTOPS_CORS_ORIGINS` (comma-separated; `cors_origin_list` strips
  trailing slashes). `"*"` is refused in `Settings._check`: the API authenticates with a header
  key, and an explicit list stops other sites from building a UI on a visitor's stored key.
  `CORSMiddleware` is added only when the list is non-empty, allows `X-API-Key`, `Content-Type`,
  `Last-Event-ID`, `X-Request-ID`, exposes `X-Request-ID` and `Retry-After`, `max_age=600`. It
  wraps the error handlers, so 4xx/429 responses carry CORS headers and the browser can read them.
- **Single-origin mode.** `AGENTOPS_WEB_DIST_DIR` mounts `StaticFiles(html=True)` at `/` *after*
  every API router, so API routes win. A missing `index.html` fails at startup instead of
  serving 404s. The image sets it, so `https://<api>/` is a working UI too.

### 2.7 Web UI (`web/src/lib/api.ts`, `web/src/App.tsx`, `web/vercel.json`, `web/src/vite-env.d.ts`)

- `API_BASE = joinBase(import.meta.env.VITE_API_BASE_URL ?? "")`; `apiUrl(path)` prefixes it.
  Empty = same origin (Vite proxy or single-origin mode). Every `fetch`, the SSE stream, the SSE
  reconnect and the terminal WebSocket (`openTerminal` derives `wss://` from `API_BASE`) use it.
- `DEMO_API_KEY = VITE_DEMO_API_KEY`: `getApiKey()` falls back to it when local storage has no
  key, so a visitor can start at once. A key the user enters wins.
- **Cold start.** `Connection.check` polls `/ready` up to `WAKE_ATTEMPTS = 20` times every
  `WAKE_RETRY_MS = 3000` ms (about a minute), shows "API waking up…" after 2.5 s without an
  answer, and explains "The server sleeps when idle and is starting up. This takes about 20
  seconds." An `alive()` guard stops the loop after unmount.
- **Copy fixes.** "Local models take 20–90 s" became "This usually takes 15–90 s", and the
  "cost (local)" label became "cost" in `Chat.tsx` and `Executions.tsx` (bug 6).
- `web/vercel.json`: Vite framework, `npm ci`, `dist`; `nosniff`, `Referrer-Policy`,
  `X-Frame-Options: DENY`; immutable caching for `/assets/*`.

### 2.8 Image and deploy script (`deploy/container/Dockerfile`, `scripts/deploy_azure.py`)

- **Dockerfile**: stage 1 `node:22-slim` builds the UI; stage 2 `python:3.13-slim` + git + uv
  0.12.19, `uv sync --frozen --no-dev --extra hosted` (dependencies first for layer caching),
  copies the UI build, bakes the embedding model into `/opt/models`, runs as uid 1000. Hosted
  defaults as `ENV` (provider, models, alternates, `reasoning_effort=low`,
  `WORKSPACE_EXEC_BACKEND=disabled`, `INDEXING_POLL_S=1800` so Neon's compute can suspend,
  `AGENT_MAX_EVIDENCE_PASSAGE_CHARS=16000` (default 30,000) so one request fits the per-minute
  token cap, `RATE_LIMIT_AGENT_PER_MIN=5` (default 30), `TELEMETRY_CAPTURE_IO=false`, rule
  routing and caches on). `CMD` runs `alembic upgrade head` then uvicorn with proxy headers.
- **`scripts/deploy_azure.py`**: checks `az`/`gh`/`docker`/`git` and `az login`; reads
  `.env.hosted` (only `AGENTOPS_*` with values; requires DB URL, LLM key, API keys); tags the image
  with the git short SHA, plus `-dirty` if the tree has uncommitted changes; logs in to GHCR with
  `gh auth token` via stdin; `docker buildx build --platform linux/amd64 --push`; checks the
  package is public (`ghcr_public`) and, if not, prints the settings link and stops; creates the
  resource group and environment (`--logs-destination none`: Log Analytics bills per GB);
  creates or updates the app with 0–1 replicas, 1 vCPU / 2 GiB, external ingress on 8000.
  Keys containing `KEY/SECRET/TOKEN/PASSWORD/DATABASE_URL` become Container Apps secrets referenced
  by `secretref:`; secret-bearing commands are logged with `quiet=True` (first four arguments only).
- `.gitignore` now ignores `.env.*` except the two examples; `.dockerignore` excludes
  `web/node_modules` and `web/dist`.

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Hosted chat API (Groq) | Hosting Ollama | No free host has the RAM/GPU for qwen3:8b at usable speed. Cost: questions and retrieved text leave the machine. |
| fastembed in-process, same nomic model | A hosted embeddings API; keeping Ollama for embeddings | Identical vectors (cosine 1.0), no vendor, no extra quota, no reindex. Cost: ~800 MB more image and RAM, which ruled out Render's 512 MB. |
| Azure Container Apps, scale 0–1 | HF Spaces (needs PRO), Render (512 MB), Oracle VM (failed), spending the $200 credit | Free grant covers demo traffic; idle is free; real HTTPS ingress and a secret store. Cost: ~15 s cold start. |
| UI on Vercel + CORS allow-list | Single-origin only | Instant page while the API wakes; the same image still supports single-origin mode. |
| Explicit origins, `*` refused | `allow_origins=["*"]` | Header-key auth plus `*` lets any site script a visitor's stored key. |
| Same-provider alternates before the fallback provider | Straight to OpenRouter | Groq meters tokens per day **per model**; another Groq model is faster and better than a free OpenRouter model. |
| Wait out hints ≤ 20 s, fall through above | Always back off; always wait | Per-minute buckets refill in seconds; a daily bucket in hours. Waiting minutes inside a request is worse than another model. |
| Request reset only when remaining-requests is 0 | Max of all reset headers | The max picked the daily request reset (hours) on token 429s, so nothing was ever waited out (bug 2). |
| 413 as a rate limit | 413 as a client error | On Groq it means "over the per-minute token cap", which another route may accept. |
| `schema` mode on Groq, `object` mode on the fallback | One mode everywhere | Not every free model supports `json_schema`; validation is the same either way. |
| Exec `disabled` | `host` backend on the PaaS | No sandbox available; host exec would run approved code next to the secrets. |
| Public demo key in the bundle | Asking visitors for a key | Zero-friction demo; a separate `demo` tenant at 5 agent runs/min limits the damage. Admin uses its own key. |
| Container Apps secrets + `secretref:` | Plain env vars; baking into the image | Values are not shown in the app's env listing or in the image. |
| Logs destination none | Log Analytics | Billed per GB; the free grant does not cover it. Cost: no hosted logs (§7). |
| Public GHCR image | Private registry with pull credentials | No registry secret to manage; the image has no secrets in it. |

---

## 4. Bugs found in live testing and how

| # | Bug | Found by | Fix |
|---|---|---|---|
| 1 | Eval runs failed with 429s; the backoff (0.5 s, 1 s…) retried long before Groq's per-minute bucket refilled | Hosted eval run | `retry_after_s` on `RateLimitError`; `retry_async` waits out hints ≤ `max_retry_after_s` |
| 2 | After fix 1 the app still never waited: taking the max of the reset headers picked `x-ratelimit-reset-requests` (~2 h, the daily request bucket) on a token 429, which exceeded the 20 s cap | Live 429 responses with both headers | `_retry_after` uses the request reset only when `x-ratelimit-remaining-requests == "0"`; pinned by the `"895"`/`"2h31m12s"`/`"7.66s"` → 7.66 case in `test_retry_after_hint_is_read_from_rate_limit_headers` |
| 3 | Testing burned Groq's 200K tokens/day for `gpt-oss-120b`; every request then fell to the slow free OpenRouter model | Quota errors mid-eval | `llm_model_*_alternates`: other Groq models, each with its own daily quota, before the fallback provider (`test_alternate_models_on_the_primary_come_before_the_fallback_provider`) |
| 4 | `deploy_azure.py` reported the public GHCR package as "private" | Deploy run | python.org Python on macOS has no system CA certificates; the SSL failure was a `URLError` and was swallowed as "private". `ghcr_public` now uses `ssl.create_default_context(cafile=certifi.where())` |
| 5 | CORS looked broken right after setting `AGENTOPS_CORS_ORIGINS` | Browser console | Not a bug: the old revision served traffic until the new one rolled out. Waiting fixed it |
| 6 | The UI said "Local models take 20–90 s" and "cost (local)" on the hosted site | Looking at the live UI | Neutral copy in `Chat.tsx`, `Executions.tsx` |
| 7 | Groq retired the Llama models the earlier plan named | Live probes of the model list, tools and JSON schema | Models picked by probing (gpt-oss-120b/20b, qwen3.8-27b, "verified 2026-11-21" in `.env.hosted.example`); `.env.example`'s fallback example changed from `llama-3.1-8b-instant` to `openai/gpt-oss-20b` |

Bug 4 is a reminder that a check whose only `except` branch returns `False` turns every
environment failure into the negative answer.

---

## 5. Security posture

- **The demo key is public by design.** It ships in the Vercel bundle (`VITE_DEMO_API_KEY`), maps
  to tenant `demo`, and is rate-limited at 5 agent runs per minute per tenant. Admin endpoints
  need the separate admin key (`AGENTOPS_ADMIN_TENANTS=admin`).
- **CORS**: explicit origins only, `*` refused at startup.
- **Secrets**: DB URL, Groq/OpenRouter keys, client API keys live as Container Apps secrets; the
  deploy script never prints their values. `.env.hosted` is git-ignored (`.env.*`).
- **No command execution** on the hosted API (§2.5).
- **Data leaves the machine**: questions and retrieved passages go to Groq (and OpenRouter on
  fallback). `docs/deployment.md` says to upload demo data only. `TELEMETRY_CAPTURE_IO=false`.
- `AGENTOPS_ENV=prod` requires API keys at startup (existing check).

---

## 6. Commands

```bash
# Dependencies for local hosted-mode runs (fastembed)
uv sync --extra hosted
# Deploy or redeploy the API (reads .env.hosted; never commit it)
cp .env.hosted.example .env.hosted
uv run python scripts/deploy_azure.py
uv run python scripts/deploy_azure.py --skip-build --tag <tag>   # settings only
# Build and run the hosted image locally (UI at http://localhost:8000)
docker build --platform linux/amd64 -t agentops-hosted -f deploy/container/Dockerfile .
docker run --rm -p 8000:8000 --env-file .env.hosted agentops-hosted
# Smoke test
API=https://agentops-api.bluecoast-6af9a19c.centralindia.azurecontainerapps.io
curl -s $API/ready
curl -s -H "X-API-Key: $KEY" $API/api/v1/models
# UI on Vercel (from web/)
vercel env add VITE_API_BASE_URL production; vercel env add VITE_DEMO_API_KEY production
vercel deploy --prod
# Gates
uv run pytest -q -W error tests/unit tests/security   # 341 passed
(cd web && npx vitest run)                             # 13 passed
```

---

## 7. What this milestone taught

1. **Free tiers are rate limiters, and the headers say which one.** Groq's 429 carries two
   buckets; reading the wrong reset turned a 7-second wait into "give up". Parse the hint, and
   decide which bucket actually refused you.
2. **Quotas are per model, so capacity is a routing problem.** Adding alternates on the same
   provider roughly tripled daily capacity without a new vendor.
3. **Memory, not CPU, picked the host.** The in-process embedding model made the image ~8×
   larger and ruled out the 512 MB tier; keeping vector parity was worth that.
4. **Same model, same vectors.** Choosing the ONNX build of the exact embedding model (verified
   by cosine 1.0) turned a vendor change into a configuration change with no reindex.
5. **Cold start is a UX problem first.** The fix that mattered was a static front door that
   explains the wait, not a warmer container.
6. **A failed check must not look like a "no".** The SSL error read as "private" for the same
   reason any catch-all `except: return False` lies.
7. **A rollout is not instant.** The CORS "bug" was the previous revision still serving.
8. **Pricing plans change.** HF Spaces and Groq's Llama models both changed under the plan; live
   probes before committing to a choice beat documentation.

---

## 8. Known limitations and open items

1. **No clean hosted baseline.** The only hosted eval (23/38) is quota-contaminated and not
   saved under `datasets/baselines/`; drift monitoring on the hosted deployment compares against
   local qwen3 baselines until a clean run is saved.
2. **Capacity.** About 2,500 tokens per question → ~80 questions/day on `gpt-oss-120b`'s 200K
   tokens, roughly triple with the alternates (build-session estimate). `qwen3.8-27b` is both
   the small model and a main alternate, so those share one quota. A full 38-case eval eats a
   large share of a day.
3. **`fallback_used`** is true for a same-provider alternate as well as for OpenRouter; traces do
   not distinguish "other Groq model" from "other vendor" except by provider name.
4. **Workspace cases cannot pass hosted.** Exec is disabled, and the workspace disk is ephemeral
   (lost on restart or scale to zero).
5. **No hosted logs** (logs destination none). Debugging relies on the API's error responses, the
   execution ledger in Postgres, and optional Langfuse tracing.
6. **Cold start ~15 s**, plus Neon compute resume after its own idle suspend.
7. **Cost reports `null`** (`pricing_source: "unavailable"`): no hosted prices configured, so
   `AGENTOPS_AGENT_MAX_COST_USD` must stay unset or runs fail closed.
8. **In-process state** (rate-limit buckets, caches, live runs) is lost on every scale-to-zero;
   max replicas is 1, so it is at least consistent while up.
9. **The demo key is only as safe as its rate limit.** A determined visitor can spend the day's
   Groq quota with 5 runs a minute.
10. **Uncommitted.** The deployed image is tagged `-dirty` if built from this tree; the code is
    not yet committed or pushed, and Vercel redeploys only from pushes to `main`.
11. **Untested paths:** `scripts/deploy_azure.py` has no tests beyond the live run; the
    `App.tsx` wake loop has no component test.

---

## 9. What I'd do differently at 100x scale

- Paid inference with real per-key budgets, and prices configured so cost limits work.
- A shared rate limiter (Redis) and a token-budget-aware router that picks a route by remaining
  quota instead of discovering exhaustion by 429.
- `min-replicas 1` (no cold start) and logs to a cheap sink.
- A sandbox service (gVisor/Firecracker) so hosted command execution can be turned back on.
- Per-visitor demo keys (short-lived, issued by a tiny endpoint) instead of one public key.
- Embeddings on a separate service or batch job so API memory does not carry the ONNX model.
- CI that deploys tagged commits, so the live image is always a clean SHA.
