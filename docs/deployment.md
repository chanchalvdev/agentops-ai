# Hosted deployment (free tier)

The local setup runs every model on Ollama. The hosted setup runs no model servers of its own:
chat models are called over HTTPS, and the only model in the container is the small embedding
model.

Live: UI **https://agentops-ai-demo.vercel.app**, API
`https://agentops-api.bluecoast-6af9a19c.centralindia.azurecontainerapps.io` (Central India).

```
Browser ──► UI on Vercel (static, always on)
              │  HTTPS + X-API-Key (CORS: only the UI's origin)
              ▼
            API on Azure Container Apps (one container, scales to zero)
              ├──► Groq API        chat models (primary)
              │      └─ 429 / 413 / 5xx / timeout ──► OpenRouter API (fallback)
              ├──► fastembed       embeddings, in-process on CPU
              └──► Neon            Postgres + pgvector
```

| Piece | Service | Cost |
|---|---|---|
| Web UI | Vercel (static build of `web/`) | Free (Hobby) |
| API | Azure Container Apps, consumption plan, 1 vCPU / 2 GiB, 0–1 replicas | Free within the monthly grant (180,000 vCPU-s, 360,000 GiB-s, 2M requests per subscription): about 50 active hours a month at this size; idle time is free |
| Container image | GitHub Container Registry, public package | Free |
| Chat models | Groq: `openai/gpt-oss-120b` (main, low reasoning effort), `qwen/qwen3.8-27b` (small) | Free tier per model: 1,000 requests/day, 8,000 tokens/min, 200,000 tokens/day |
| Fallback | OpenRouter `google/gemma-4-31b-it:free` | Free tier |
| Embeddings | fastembed, `nomic-embed-text` v1.5, in the container | Free |
| Postgres + pgvector | Neon | Free plan |

The same image also serves the UI itself at `/` (single-origin mode), so `https://<api>/` works
too; Vercel is the showcase front door because it loads instantly while the API may be asleep.

## What changes compared with local

- **Data leaves the machine.** Questions and retrieved document text go to Groq (and OpenRouter
  on fallback). Upload demo data only.
- **Command execution is off** (`AGENTOPS_WORKSPACE_EXEC_BACKEND=disabled`). Container Apps
  cannot run the Docker sandbox, so the agent is not offered `workspace_exec` and the terminal
  and `POST /workspace/exec` refuse every command. Workspace files still work, and the disk is
  ephemeral: workspace files are lost on restart. Everything else lives in Postgres.
- **Structured output** uses Groq's native JSON-schema mode (`AGENTOPS_LLM_JSON_MODE=schema`);
  the OpenRouter fallback uses JSON-object mode with the schema in the prompt
  (`AGENTOPS_FALLBACK_JSON_MODE=object`). Every output is validated against the schema either way.
- **Free-tier quotas are the limit.** Groq caps requests and tokens per minute and per day per
  model, and one agent run makes several model calls. The image lowers evidence per prompt to
  16,000 characters so one request fits the per-minute token cap, and agent runs to 5 per minute
  per tenant. On a 429 the router reads Groq's wait hint (`retry-after`, else
  `x-ratelimit-reset-tokens`, or the request reset when the daily request count is spent). A
  wait of up to `AGENTOPS_LLM_MAX_RETRY_AFTER_S` (20 s) is waited out; a longer one (a spent
  daily quota) skips straight to the next route. Routes run in order: the main model, the
  `AGENTOPS_LLM_MODEL_MAIN_ALTERNATES` on Groq (each model has its own daily quota), then
  OpenRouter. A 413 (request over the per-minute token cap) is treated the same way.
- **Capacity.** One question uses about 2,500 tokens, so `gpt-oss-120b` alone answers about 80
  questions a day; the alternates (`gpt-oss-20b`, `qwen3.8-27b`) roughly triple that before the
  OpenRouter fallback.
- **Cold start.** With zero replicas the first request after idle starts the container
  (about 10–20 s); the UI shows "waking up" and retries. The embedding model is baked into the
  image, so nothing downloads at start.
- **Neon's compute suspends** when idle; the indexing worker (crash recovery only) polls every
  30 minutes so it can.
- **Cost reports `null`** (`pricing_source: "unavailable"`): no hosted prices are configured.
  Leave `AGENTOPS_AGENT_MAX_COST_USD` unset, or every run fails closed.

## One-time setup

1. **Neon** (neon.tech): create a project → *Connect* → turn *Connection pooling* off → copy the
   URL as is (`postgresql://…?sslmode=require&channel_binding=require` is fine).
2. **Groq** (console.groq.com) → *API Keys* → create.
3. **OpenRouter** (openrouter.ai) → *Keys* → create; pick a current `:free` model.
4. **Client keys**: `openssl rand -hex 16`, twice: one public demo key (tenant `demo`, built into
   the UI) and one admin key (tenant `admin`) for you.
5. **Azure**: set a budget alert (*Cost Management* → *Budgets*, e.g. $5), then
   `brew install azure-cli && az login`. A free-trial subscription must be upgraded to
   pay-as-you-go when its 30-day credit ends, or Azure disables it; the monthly free grant
   continues after the upgrade.
6. **GitHub**: `gh auth refresh -s write:packages` (lets the script push the image).
7. `cp .env.hosted.example .env.hosted` and fill it in. The file is git-ignored.

## Deploy the API

```bash
uv run python scripts/deploy_azure.py
```

It builds `deploy/container/Dockerfile` for linux/amd64, pushes it to
`ghcr.io/<github-user>/agentops-ai:<git sha>`, creates the resource group, a Container Apps
environment with logs off (Log Analytics bills per GB), and the app, with secrets stored as
Container Apps secrets. It prints the API URL.

The visibility check uses certifi's CA bundle (python.org builds on macOS have no system root
certificates). The first push creates a **private** package; the script stops and prints the link to make it
public (*Package settings* → *Change visibility* → *Public*), then rerun with the `--skip-build
--tag <tag>` it prints. Later deploys: rerun the script; `--skip-build` only updates settings.

Check: `curl https://<api>/ready` → `{"status":"ready","checks":{"database":"ok"}}`.

## Deploy the UI

On vercel.com: *Add New → Project* → import the GitHub repo, then:

- **Root Directory**: `web` (framework, build command and output come from `web/vercel.json`)
- **Environment Variables**:
  - `VITE_API_BASE_URL` = `https://<api>` (no trailing slash)
  - `VITE_DEMO_API_KEY` = the demo key (it ships in the page: it is public by design and only
    as strong as the demo tenant's rate limits)

Or from the CLI: `cd web && vercel link --project agentops-ai`, `vercel env add` both variables
for production, `vercel deploy --prod`, and `vercel domains add <name>.vercel.app` for a stable
address. Deploy, then add the Vercel URL to `.env.hosted` as `AGENTOPS_CORS_ORIGINS` and run
`uv run python scripts/deploy_azure.py --skip-build`. Vercel redeploys on every push to `main`.

## Check it

```bash
API=https://<api>; KEY=<demo key>
curl -s $API/ready
curl -s -H "X-API-Key: $KEY" $API/api/v1/models
curl -s -H "X-API-Key: $KEY" -F file=@datasets/corpus/adr-003-vector-store.md $API/api/v1/documents
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"question":"Why was pgvector chosen?"}' $API/api/v1/agent/run
```

Then open the Vercel URL: it should connect with the demo key, show "waking up" if the API was
asleep, and answer with citations.

## Run the image locally

```bash
docker build -t agentops-hosted -f deploy/container/Dockerfile .
docker run --rm -p 8000:8000 --env-file .env.hosted agentops-hosted   # UI at http://localhost:8000
```

## Switching providers

Any OpenAI-compatible API works as primary or fallback: set `AGENTOPS_LLM_BASE_URL`,
`AGENTOPS_LLM_API_KEY` and the two model names (e.g. Cerebras, `https://api.cerebras.ai/v1`).
Use `AGENTOPS_LLM_JSON_MODE=schema` only for models that accept a JSON-schema
`response_format`; for reasoning models (e.g. `openai/gpt-oss-120b`) set
`AGENTOPS_LLM_REASONING_EFFORT=low`.

The committed evaluation baselines were measured on local qwen3 models. After deploying, run an
evaluation as the admin tenant and save it as the hosted baseline before relying on drift alerts.
