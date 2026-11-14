# End-to-end demo

Everything below runs locally: Postgres in Docker, models in Ollama, the API, and the web UI. It
takes about 15 minutes, most of it waiting on the local models (each agent answer takes 20–90 s
on an Apple M4 with 16 GB).

## 1. Start the stack

```bash
ollama serve &                                   # skip if Ollama is already running
ollama pull qwen3:8b && ollama pull qwen3:1.7b && ollama pull nomic-embed-text

docker compose up -d db                          # Postgres 17 + pgvector on localhost:5433
uv sync && uv run alembic upgrade head
uv run uvicorn --factory app.main:create_app --port 8000

cd web && npm install && npm run dev             # http://localhost:5173
```

Open http://localhost:5173 and paste the development key `dev-key-tenant-a` in the sidebar. The
sidebar should show **API ready** and **tenant tenant_a**. (Port conflicts and other setup issues:
README, *Troubleshooting*.)

## 2. Documents

**Documents → Upload** each file in `datasets/corpus/` except `vendor-notice-card-vault.md` (a
prompt-injection fixture for the evaluation set). Status moves from `pending` to `ready` as the
background indexer chunks and embeds each file. Upload the same file again: the API recognises
identical content and returns the existing document instead of indexing it twice.

## 3. A grounded, cited answer

**Chat**, mode `graph`: *What is the RPO in the latest payments architecture?*

- The left column streams every step as it happens: `classify` and `plan` on qwen3:1.7b, three
  `document_search` calls, tool selection and the answer on qwen3:8b, citation validation, and
  the `verify` call that checks the answer against its cited passages.
- The answer says 5 minutes and cites `payments-architecture-v2.md`. Click the citation: the
  drawer shows the exact chunk the answer came from (section, chunk index, document version).
- Ask *What is the RPO in v2, in seconds?*: the calculator is called, and its result appears as a
  `[T1]` reference you can open.
- Switch the mode to `loop` and ask again to compare the hand-written agent loop with the graph.

## 4. Human approval for a workspace change

**Chat**: *Create a new file notes/ui-demo.md in my workspace containing the line: UI demo*

The run stops at `human_approval` with status `awaiting_approval`. Nothing has been written.
Click **Review in Approvals**:

- The preview shows the change as a diff against the current file (here: a new file, one added
  line). For an edit it shows exactly which lines change; if the text to replace is not in the
  file, it says the tool will refuse.
- **Approve**: the run resumes from its Postgres checkpoint, performs the write, and answers with
  a `[T1]` reference to the write receipt. **Reject** (with a reason): nothing is written.

During a rehearsal the agent proposed an edit that would have duplicated a line in a file that
already had the function. The diff made that obvious, and it was rejected. That is the point of
reviewing a preview instead of approving blind.

## 5. The sandbox

**Workspace**: the file tree now has `notes/ui-demo.md`. In the terminal:

| Command | Result |
|---|---|
| `cat notes/ui-demo.md` | the file, `exit 0` |
| `rm -rf calc` | `command_not_allowed` (allowlist) |
| `cat ../../.env` | `workspace_access_denied` (path jail) |
| `ls calc \| wc -l` | `invalid_request` (no shell: no pipes, redirects, `;`, `&&`) |

The terminal uses the same executor and policy as the agent's `workspace_exec` tool. **Audit log**
lists every operation, including those refusals.

## 6. Traces

**Executions** lists your runs. Select one for its ledger timeline: each step, model call (model,
tokens, latency), tool call (with cache hits marked), approval, and correction. Click a row for its
recorded data.

## 7. Evaluation and monitoring (operator)

`tenant_a` is an operator in development (`AGENTOPS_ADMIN_TENANTS`).

- **Evaluation runs**: the committed Day 4 runs show baseline 33/38 and optimized 36/38. Compare
  them: pass rate 87% → 95%, no threshold breaches, three newly passing tool-use cases.
- **Monitoring → Run check now**: a drift check over quality, retrieval, data, and operational
  signals, persisted with one alert per breach. If the latest retrieval report is the
  `degraded-chunking` probe, answer coverage is flagged as measured drift (0.971 → 0.882).

Change the key to `dev-key-tenant-b`: the evaluation screens explain that this tenant is not an
operator, the workspace refuses access (403, and the terminal closes with 4403), and tenant A's
executions and documents are invisible.

## 8. The same flow from a terminal

```bash
scripts/demo.sh                 # pauses at the approval and prints how to decide
APPROVE=1 scripts/demo.sh       # approves the write itself
AGENTOPS_URL=http://localhost:8010 scripts/demo.sh   # API on another port
```

It uploads the corpus, asks a cited question, opens its source passage, prints the trace, requests
and (optionally) approves a workspace write, shows the sandbox refusals and tenant isolation, and
runs a drift check, using only public API endpoints.
