# Journal 12 — API Additions for the Web UI (Day 5, M12)

Milestone: the first part of Day 5 in product.txt §18 — "Web UI (Section 24) over the finished
API". §24 says the UI "talks only to the public API", so before writing screens the build session
listed what each screen needs and found three gaps: no way to list a tenant's executions, no way
to open the passage a citation points at, and an operator evaluation list that hid runs started
from the CLI. M12 closes those three in the API, with tests, so the UI never has to hold logic or
data the API lacks. Branch `day-5-ui-demo`; the code is in commit `cff28ed` ("feat: Day 5 web UI,
end-to-end demo, and loop-vs-graph comparison", 14 Nov). Written by the Recorder agent from the
code, `tests/integration/test_ui_api.py`, `reports/ui-api.log` and test output; facts reported by
the build session and not visible in a file are marked as such.

Status (re-run by the Recorder on 14 Nov):
- `uv run pytest -q -W error` → **373 passed, 7 deselected** (live Ollama) in 21.6 s, with
  Postgres available again (the Day 4 Recorder could not run the DB tests; this time it could).
- `tests/integration/test_ui_api.py` → **3 passed**.
- `ruff check app tests` → all checks passed; `ruff format --check` → 132 files formatted;
  `mypy app` (strict) → no issues in 98 source files.

Not in this milestone: pagination cursors for executions, server-side search/filter beyond
`status`, a chunk endpoint that returns neighbouring context, per-operator scoping of evaluation
runs (operators see all runs by design).

---

## 1. What was built

### `GET /api/v1/executions` (`app/api/agent.py:list_executions`, `app/agents/store.py:recent`)
- Returns the caller's most recent executions, **newest first**, as `list[ExecutionOut]` (same
  schema as `GET /executions/{id}`).
- Query: `limit` (1–200, default 50; `0` → 422), `status` (≤ 32 chars, exact match; an unknown
  status returns `[]`, not an error).
- `ExecutionStore.recent(tenant_id, limit, status)`: one `SELECT … WHERE tenant_id = :t
  [AND status = :s] ORDER BY created_at DESC LIMIT :n`. Tenant scoping is in the store, not the
  route — the same rule as every other read since Day 1.
- No rate class: it is a read, like the other execution reads (journal 11 §1).

### `GET /api/v1/documents/{id}/chunks/{chunk_id}` (`app/api/documents.py:get_chunk`, `app/ingestion/pipeline.py:IngestionService.chunk`)
- Returns `ChunkOut`: `id, document_id, filename, document_version, chunk_index, section,
  page_start, page_end, char_start, char_end, text`. This is what the Chat screen's citation drawer
  shows.
- One query joins `Document` and `DocumentChunk` and requires **both** `Document.tenant_id` and
  `DocumentChunk.tenant_id` to equal the caller's tenant, plus the document/chunk ids. Anything else
  — missing chunk, wrong document id, another tenant's chunk — is the same
  `NotFoundError("Chunk not found")` → **404**. Same rule as Day 1's documents (404, not 403, so
  ids can't be probed; interview 01 Q35) and same defence in depth as `scoped_select` (both
  tables carry `tenant_id`; interview 01 Q14).
- Citations already carried `document_id` and `chunk_id` (Day 2 `AgentRunResult`), so the UI
  needs no mapping of its own.

### Operators see every evaluation run (`app/api/evaluations.py`, `app/evaluation/store.py`)
- `EvaluationStore.list_runs(requested_by: str | None, …)` and `report(requested_by: str | None,
  run_id)`: `None` means "no `requested_by` filter".
- The admin-only routes (`AdminDep`, journal 11) now pass `None`: `GET /evaluations/runs`,
  `GET /evaluations/runs/{id}`, `GET /evaluations/compare`.
- `EvaluationRunOut` gains `requested_by` so the UI can show where a run came from.
- Why: the runner records `requested_by = "cli"` for `agentops eval run`, and the API filtered by
  the caller's tenant id. Every committed live run (Day 3 baseline, Day 4 baseline and optimized,
  the loop run) was started from the CLI, so **an operator's run list in the UI was empty**. This
  was found by driving the Evaluations screen against the live API (journal 13 §4), not by a test.
- Non-operators still get **403** (`require_admin` is unchanged).

---

## 2. Files touched

Modified: `app/agents/store.py` (`recent`, +13), `app/api/agent.py` (`list_executions`, +14),
`app/api/documents.py` (`ChunkOut`, `get_chunk`, +38), `app/ingestion/pipeline.py` (`chunk`, +22),
`app/api/evaluations.py` (`requested_by`; `None` filter, +7/−4), `app/evaluation/store.py`
(optional filter, +12/−11), `README.md` (two endpoint rows).

New: `tests/integration/test_ui_api.py` (79 lines, 3 tests).

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Add read endpoints to the API | Let the UI query the trace of known ids, or keep a client-side list of runs | §24: the UI talks only to the public API; a list the browser kept would be per-browser and lie after a CLI run. |
| `GET /executions` returns `ExecutionOut` | A new slimmer summary schema | One schema, already tested; the extra fields (tokens, counters) are what the Executions screen shows. |
| Fixed `limit`, newest first, optional `status` | Cursor pagination, full-text search | Enough for one user's recent runs; cursor pagination is the scale answer (§9). |
| Chunk lookup by `(document_id, chunk_id)` | By `chunk_id` alone | The citation has both; requiring the pair means a mismatched id is just a 404 and the query uses the document's tenant check too. |
| 404 for another tenant's chunk | 403 | No existence oracle — consistent with documents and executions since Day 1. |
| Return chunk `text` | Return only offsets and let the UI fetch the document | The UI would need the whole extracted text; the chunk is exactly what was cited. |
| Operators see every run (`None` filter) | Record CLI runs as the admin tenant; filter by tenant | Evaluation is an operator function across tenants (journal 11); the runner is not a tenant. Keeps `requested_by` truthful ("cli"). |
| Method named `recent` | `list` (first attempt) | See §4: `list` shadowed the builtin inside the class body. |

---

## 4. Mistakes and how each was caught

### 4.1 A method named `list` broke the module at import (reported by the build session)
The first version of the new store method was `async def list(...)` on `ExecutionStore`. Inside a
class body, a `def list` rebinds the name `list` for the rest of that body. Annotations are
evaluated at function definition time (`app/agents/store.py` has no `from __future__ import
annotations`), so every later method in the class whose signature said `list[...]` was now
subscripting a function → `TypeError` at import, and the whole app failed to start. Renamed to
`recent`. Visible evidence: the shipped name is `recent`, and `ApprovalStore.list` (line 146) and
`IngestionService.list` (pipeline line 288) both still exist — they work only because no later
annotation in their class bodies uses `list[...]` after them (the methods that follow are
`delete`, `mark_for_reindex` and `decide`). A latent trap, not a bug today; noted in §8.

### 4.2 The operator run list was empty (found by driving the UI)
See §1. The evaluation API had been tested since Day 3 with runs the test itself started through
the API as `tenant_a`, so `requested_by == tenant_id` always held in tests. Only real data (CLI
runs) showed the gap. Regression test: `test_operators_see_evaluation_runs_started_from_the_cli`
starts a run with `requested_by="cli"` directly in the store and asserts it is listed with
`requested_by == "cli"`, its detail returns 200, and tenant B still gets 403.
`reports/ui-api.log` shows the API restarted at 20:43:23 UTC on 8010 and the Evaluations screen
then calling `GET /api/v1/evaluations/runs` → 200.

---

## 5. How it was verified

- `test_executions_list_is_newest_first_and_tenant_scoped`: two runs; the list's first two ids
  are `[second, first]`; `question` and `status` match; `limit=1` → one row; `status=nope` → `[]`;
  tenant B → `[]`; `limit=0` → 422.
- `test_citation_chunk_can_be_opened_by_its_owner_only`: a real `/agent/run` (fake LLM) answers
  with a citation; the chunk endpoint returns `filename == "dr.md"`, text containing "5 minutes"
  and starting with the citation's snippet, `char_end > char_start`; tenant B → 404; a random
  document id with the right chunk id → 404.
- `test_operators_see_evaluation_runs_started_from_the_cli` (§4.2).
- Live: the UI's Executions screen and citation drawer, and `scripts/demo.sh` step 4 ("Open the
  first citation's source passage") in `reports/demo-run.log`, which printed
  `payments-architecture-v2.md § Availability and Recovery | Data Retention | Migration Plan` and
  the chunk text containing "Recovery Point Objective (RPO): 5 minutes".

---

## 6. Commands

```bash
H='X-API-Key: dev-key-tenant-a'
curl -s -H "$H" 'localhost:8000/api/v1/executions?limit=5&status=answered'
curl -s -H "$H" localhost:8000/api/v1/documents/$DOC/chunks/$CHUNK
curl -s -H "$H" localhost:8000/api/v1/evaluations/runs    # operator: includes requested_by "cli"
uv run pytest -q -W error tests/integration/test_ui_api.py
```

---

## 7. What this milestone taught

1. **Build the client's data needs into the API first.** Listing what each screen reads exposed
   two missing endpoints before a line of UI was written; the UI then stayed a thin client.
2. **Tests that create their own data can hide an access rule.** Every eval API test started its
   own run as `tenant_a`, so a filter on `requested_by == tenant` looked correct for three days.
   Real CLI-created data broke it on first contact.
3. **Python's class-body scoping is a real hazard.** A method called `list` silently rebinds the
   builtin for the rest of the class; with eager annotations it fails at import, which at least is
   loud.
4. **Keep the isolation rule the same everywhere.** The chunk endpoint copies the documents rule
   (both tenant columns, 404 across tenants) instead of inventing a new one.

---

## 8. Known limitations and open findings (Recorder review)

1. **`ApprovalStore.list` and `IngestionService.list` still shadow the builtin** inside their
   classes. Harmless today only because nothing after them annotates `list[...]`; adding a method
   below them with such an annotation would break import. Renaming them (or `from __future__ import
   annotations`) removes the trap.
2. **`GET /executions` has no cursor** — only the latest 200 are reachable through it.
3. **`status` is a free string** (≤ 32 chars); an invalid value returns `[]` rather than 422. Fine
   for a UI filter, less helpful for API users.
4. **Operators see every run, including runs started by other operators**, with no per-run access
   rule. Matches the "admin is a tenant list, not a role" limitation of journal 11 §8 item 7.
5. **The chunk endpoint returns the chunk as stored now.** Chunk ids are deterministic
   (`stable_chunk_id` = uuid5 of document id, chunking config, index and text hash), so a reindex
   with the same configuration keeps old citations working. A chunking-config change produces new
   ids, and deleting the document removes them; then an old citation 404s and the execution keeps
   only the 300-char snippet. A new version is a new document id, so v1 citations keep opening v1
   while it exists.

---

## 9. What I'd do differently at 100x scale

- Cursor pagination (`created_at, id`) and an index on `(tenant_id, created_at DESC)` for the
  execution list; filters for mode, date range and source.
- Keep cited chunks addressable across chunking-config changes and deletions (retain superseded
  chunks, or store the cited text with the execution) so old citations still open.
- Roles and scopes for evaluation access (operator vs tenant admin), with `requested_by` as a real
  principal rather than `"cli"`.
