# Interview Prep 12 — API Additions for the Web UI (Day 5, M12)

Every answer is grounded in branch `day-5-ui-demo` (code in commit `cff28ed`),
`tests/integration/test_ui_api.py`, `reports/ui-api.log` and `reports/demo-run.log`. Format:
direct answer, then the reasoning, then a code pointer. Companion: `docs/journal/12-ui-api.md`.

---

## Basic

### Q1. What did M12 add to the API?
**Answer:** Two read endpoints — `GET /api/v1/executions` (the caller's recent runs, newest first,
`limit` and `status`) and `GET /api/v1/documents/{id}/chunks/{chunk_id}` (one cited passage) — and
a change so operators see every evaluation run, including runs started from the CLI, with
`requested_by` in the run list.
Pointer: `app/api/agent.py:list_executions`; `app/api/documents.py:get_chunk`;
`app/api/evaluations.py:list_runs`.

### Q2. Why change the API at all for a UI milestone?
**Answer:** product.txt §24 says the UI talks only to the public API. The Executions screen needs a
list of runs and the Chat screen needs to open a citation's source; neither existed. Adding them to
the API means the CLI, scripts and any other client get them too, and the UI holds no data or rule
of its own.
Pointer: product.txt §24; `web/src/lib/api.ts` header comment.

### Q3. What does the chunk endpoint return?
**Answer:** `ChunkOut`: chunk id, document id, filename, document version, chunk index, section,
page range, character offsets and the chunk text — what the citation drawer shows.
Pointer: `app/api/documents.py:ChunkOut`.

### Q4. What does another tenant get for my chunk?
**Answer:** 404 `Chunk not found`, the same as a chunk that doesn't exist. The query requires both
`Document.tenant_id` and `DocumentChunk.tenant_id` to match the caller.
Pointer: `app/ingestion/pipeline.py:IngestionService.chunk`;
`test_citation_chunk_can_be_opened_by_its_owner_only`.

---

## Logical

### Q5. Why 404 rather than 403 across tenants?
**Answer:** 403 confirms the id exists, which lets an attacker enumerate ids. One answer for
"missing" and "someone else's" leaks nothing. It's the same rule documents and executions have
used since Day 1.
Pointer: interview 01 Q35; `tests/integration/test_ui_api.py`.

### Q6. Why look up a chunk by `(document_id, chunk_id)` instead of `chunk_id` alone?
**Answer:** A citation already carries both. Requiring the pair means a mismatched document id is
just a 404, and the query applies the document's tenant check as well as the chunk's — defence in
depth, like `scoped_select` in retrieval.
Pointer: `IngestionService.chunk` (join on `DocumentChunk.document_id == Document.id`).

### Q7. Why was the operator's evaluation list empty?
**Answer:** The store filtered runs by `requested_by == caller's tenant`. The CLI records
`requested_by = "cli"`, and every committed live run was started from the CLI. The API tests always
started their own runs as `tenant_a`, so the filter looked right until the UI showed real data.
Fix: the admin-only routes pass `None` (no filter).
Pointer: `app/evaluation/store.py:list_runs`, `report`;
`test_operators_see_evaluation_runs_started_from_the_cli`.

### Q8. Isn't "operators see everything" a permission widening?
**Answer:** Only for operators, who already run evaluations across tenants and read cross-tenant
monitoring (journal 11). Non-operators still get 403. The run's `requested_by` stays truthful
("cli") instead of pretending the CLI was a tenant.
Pointer: `app/api/deps.py:require_admin`.

### Q9. Why is an unknown `status` filter `[]` and not 422?
**Answer:** It's an exact-match string filter (≤ 32 chars). For a UI filter, empty is a fine
answer; for API users a 422 with the allowed values would be friendlier — listed as a limitation.
Pointer: `app/api/agent.py:list_executions`.

---

## Advanced

### Q10. Tell me about the method named `list`.
**Answer:** The new store method was first called `list`. Inside a class body `def list` rebinds
the name, and without `from __future__ import annotations` annotations are evaluated when each
`def` runs, so the next method annotated `-> list[ExecutionEvent]` subscripted a function and the
module failed at import. Renamed to `recent`. Two other classes still have a `list` method and
work only because nothing after them uses `list[...]` — a trap worth removing.
Pointer: `app/agents/store.py:ExecutionStore.recent`, `ApprovalStore.list`;
`app/ingestion/pipeline.py:IngestionService.list`.

### Q11. What happens to a citation after its document is reindexed?
**Answer:** With the same chunking config, nothing: chunk ids are uuid5 of document id, config,
index and text hash, so the reindex reproduces them and the citation still opens. After a
chunking-config change (new ids) or a delete, the old pair 404s and only the execution's 300-char
snippet remains. Fixes: keep superseded chunks, or store the cited text with the execution.
Pointer: `app/ingestion/chunking.py:stable_chunk_id`; journal 12 §8 item 5.

### Q12. How does the test prove the drawer shows the right passage?
**Answer:** It runs a real agent request (fake LLM), takes the first citation, fetches the chunk
and asserts the filename, that the text contains "5 minutes", that it starts with the citation's
snippet, and that the offsets are sane; then tenant B and a random document id both get 404.
Pointer: `test_citation_chunk_can_be_opened_by_its_owner_only`.

---

## Scenario

### Q13. "The Evaluations screen says there are no runs, but I ran one from the CLI."
**Answer:** That was the M12 bug: the API filtered by the caller's tenant and CLI runs are
`requested_by = "cli"`. After the fix, operators see every run, with `requested_by` shown. If it
still happens: check the key's tenant is in `AGENTOPS_ADMIN_TENANTS` (otherwise it's a 403, which the
screen explains) and that the CLI and API use the same database.
Pointer: `app/api/evaluations.py`.

### Q14. "A user clicked a citation and got 'not found'."
**Answer:** Either the document was deleted or reindexed under a different chunking config since
the answer (so that chunk id no longer exists), or the key belongs to a different tenant than the
one that ran the question. Both return the same 404 by design.
Pointer: `IngestionService.chunk`.

---

## Trade-off

### Q15. Reuse `ExecutionOut` for the list, or a slimmer schema?
**Answer:** Reused: one tested schema, and the Executions screen shows its counters and tokens
anyway. Cost: larger responses (answer text, citations) per row; at scale a summary schema plus
cursor pagination is better.
Pointer: `app/api/agent.py:ExecutionOut`.

### Q16. Store cited text with the execution, or fetch the chunk live?
**Answer:** Live fetch is what shipped: no duplication, and the drawer shows chunk metadata. It
breaks after a delete or a chunking-config change. Storing the cited text makes old answers self-contained at the cost of
storage and of keeping text out of telemetry.
Pointer: journal 12 §3, §8.

---

## Justification

### Q17. "Why not keep a list of my runs in the browser instead of a new endpoint?"
**Answer:** It would be per-browser, miss runs from the CLI or another device, and duplicate state
the server already has. §24 asks for a thin client.

### Q18. "Why a regression test for a one-line filter change?"
**Answer:** Because the bug existed for three days precisely because tests created their data
through the same path the filter assumed. The new test creates a run the way the CLI does.

---

## System design

### Q19. How would these endpoints change at 100x scale?
**Answer:** Cursor pagination on `(created_at, id)` with a `(tenant_id, created_at DESC)` index;
more filters; immutable cited chunks; roles and scopes for evaluation access with a real principal
in `requested_by`.
Pointer: journal 12 §9.
