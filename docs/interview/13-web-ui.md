# Interview Prep 13 — The React Web UI (Day 5, M13)

Every answer is grounded in `web/` on branch `day-5-ui-demo` (commit `cff28ed`),
`reports/ui-api.log`, `reports/ui-vite.log` and the build session's report of the Chrome session.
Format: direct answer, then the reasoning, then a code pointer. Companion:
`docs/journal/13-web-ui.md`.

---

## Basic

### Q1. What is the web UI, in one sentence?
**Answer:** A React + TypeScript + Vite + Tailwind single-page app with six screens (Chat,
Documents, Workspace, Approvals, Executions, Evaluations & Monitoring) that is a pure client of the
public API: it holds only the user's API key and enforces nothing itself.
Pointer: `web/src/App.tsx`; `web/src/lib/api.ts` header comment.

### Q2. What are its dependencies?
**Answer:** At runtime only `react` and `react-dom`. Build/test: TypeScript, Vite, the React
plugin, Tailwind/PostCSS/autoprefixer, Vitest. The production JS bundle is 193 KB, 60 KB gzipped.
Pointer: `web/package.json`; `web/dist/assets/`.

### Q3. How does the browser reach the API without CORS?
**Answer:** Vite proxies `/api` (including the terminal WebSocket, `ws: true`), `/health` and
`/ready` to `AGENTOPS_API_URL` in both `dev` and `preview`, so the page and the API share one
origin. The API needed no CORS change.
Pointer: `web/vite.config.ts`.

### Q4. Where does the API key live?
**Answer:** In `localStorage` (`agentops.apiKey`), sent as `X-API-Key` on every request and as the
first WebSocket frame for the terminal. It is the only credential, as §24 requires.
Pointer: `web/src/lib/api.ts:getApiKey`, `openTerminal`.

### Q5. What does the Chat screen show while a run is in progress?
**Answer:** A timeline of `step`, `model_call` (model, tokens, latency), `tool_call`,
`correction` and `approval` events as they arrive over SSE, then the answer with `[S#]` links that
open the source chunk and `[T#]` links that show the tool result; if the run pauses, a "Review in
Approvals" link.
Pointer: `web/src/screens/Chat.tsx`; `web/src/lib/format.ts:splitCitations`.

---

## Logical

### Q6. Why not `EventSource` for the agent stream?
**Answer:** `EventSource` only issues GET and can't set headers. The stream is
`POST /agent/run/stream` with a JSON body and an `X-API-Key` header. So it's `fetch`, a
`TextDecoderStream`, and an incremental parser.
Pointer: `web/src/lib/api.ts:streamAgent`; `web/src/lib/sse.ts`.

### Q7. What does the SSE parser have to handle?
**Answer:** Events split across network chunks (keep a buffer), `\r\n` and `\r` line endings,
comment lines like the server's `: keep-alive` (drop), several `data:` lines (join with `\n`), and
a missing `event:` (default `message`). A message ends at a blank line. All of these are unit
tests.
Pointer: `web/src/lib/lib.test.ts` ("SseParser").

### Q8. What happens when the user presses Stop?
**Answer:** The fetch is aborted. To the server that's a client disconnect, which Day 2 made into
a persisted `cancelled` execution: the stream's `finally` cancels the run task, which saves its
status and re-raises.
Pointer: `Chat.tsx` (`AbortController`); interview 05 Q21.

### Q9. How does the approval preview work?
**Answer:** It reads the current file through the API. For `workspace_write` the new content is
the proposal (empty "before" for a new file); for `workspace_edit` it replaces the first
occurrence of `old` with `new`, or says "the tool will refuse this edit" if `old` isn't there. An
LCS line diff with 3 lines of context is shown. It is display only — the server applies the change
on approve.
Pointer: `web/src/lib/diff.ts`; `web/src/screens/Approvals.tsx:Preview`.

### Q10. How does the UI handle a non-operator on the Evaluations screen?
**Answer:** The API returns 403 `permission_denied`; the screen recognises it and explains that
evaluations and monitoring are operator functions (shared model for minutes, cross-tenant data)
and how a tenant becomes one (`AGENTOPS_ADMIN_TENANTS`).
Pointer: `web/src/screens/Evaluations.tsx` (`forbidden`); `components/ui.tsx:ErrorBox`.

### Q11. How can a screen avoid showing a stale response?
**Answer:** `useLoad` numbers each request and applies a result only if it is still the latest, so
a slow earlier request can't overwrite a newer one. Changing the API key remounts the screen, so
nothing from the previous tenant remains.
Pointer: `web/src/components/ui.tsx:useLoad`; `App.tsx` (`key={apiKey:screen}`).

---

## Advanced

### Q12. Tell me about the bad edit you caught.
**Answer:** Asked to add a function, qwen3:8b proposed a `workspace_edit` to a file that already
had it. The diff showed the edit would duplicate a `return` line, so I rejected it with a reason.
Nothing on the server would have refused it — the `old` text existed exactly once. It's the same
behaviour the Day 3 eval case `ws-edit-approved` measures, seen from the reviewer's chair.
Pointer: journal 13 §4.1; `docs/demo.md` §4.

### Q13. And then the resumed run failed?
**Answer:** Yes: after the reject, the graph resumed to finish its answer and its `verify_answer`
call failed with `provider_unavailable` — the API log shows it. macOS had killed the Ollama server
under memory pressure (swap around 10 GB). I restarted Ollama and added the symptom to the README.
The API's status was right; the machine was the failure.
Pointer: `reports/ui-api.log` (`node: verify_answer, code: provider_unavailable`); README
"Troubleshooting".

### Q14. What was "destroy is not a function"?
**Answer:** React calls whatever an effect returns as its cleanup. The terminal effect returned
something that wasn't a function, so unmounting the Workspace screen threw and blanked the whole
app. Fix: explicit effects that return a cleanup function, plus a per-screen error boundary so one
broken screen shows an error and keeps the navigation.
Pointer: `web/src/screens/Workspace.tsx` (terminal effect); `App.tsx:ScreenBoundary`.

### Q15. Why did "Terminal closed (1006)" appear on every visit?
**Answer:** StrictMode in development mounts, unmounts and remounts each effect. The first socket
was closed by the cleanup, but its `onclose` handler still wrote into the terminal log of the new
mount. The cleanup now sets `onmessage` and `onclose` to null before closing the socket.
Pointer: `Workspace.tsx` cleanup ("Detach first").

### Q16. Why was the token total wrong?
**Answer:** The UI read `usage.total_tokens`. In Python that's a `@property` on the `Usage` model,
and Pydantic doesn't serialize properties, so the JSON had no such field. The UI now sums input and
output tokens. The lesson is hand-written TypeScript types drift from the API; generate them from
OpenAPI.
Pointer: `app/llm/schemas.py:Usage.total_tokens`; `web/src/screens/Chat.tsx`.

---

## Scenario

### Q17. "The UI says the API is unreachable."
**Answer:** Check the proxy target: `AGENTOPS_API_URL` defaults to `localhost:8000`. On this
machine 8000 belonged to another project's server, so the API ran on 8010 and the UI was started
with `AGENTOPS_API_URL=http://localhost:8010 npm run dev`. Then `/ready` for database and Ollama.
Pointer: README "Troubleshooting"; `web/vite.config.ts`.

### Q18. "A tenant says they can approve another tenant's write through the UI."
**Answer:** They can't through any path: the UI has no logic of its own; approvals are listed and
decided by the API with the caller's tenant, and a decision is a conditional UPDATE scoped to the
tenant. The UI could at most show what the API returns. I'd reproduce with curl and the two keys.
Pointer: `app/agents/store.py:ApprovalStore.decide`; interview 05 Q17.

### Q19. "Security wants the API key out of localStorage."
**Answer:** Fair for a hosted deployment: any XSS can read it. The step up is an OIDC login with
short-lived tokens in httpOnly cookies, CSRF protection and a CSP; API keys remain for machines.
For a local developer tool, the documented risk was accepted.
Pointer: README "Known limitations".

---

## Trade-off

### Q20. Client-side diff vs a server preview endpoint?
**Answer:** Client-side was cheap and enough to catch the duplicated line. But it re-implements the
tool's "first occurrence" rule and diffs against the file as it is now, not when the tool runs. A
server-side dry run by the tool itself would be exact.
Pointer: `web/src/lib/diff.ts:applyEdit` comment.

### Q21. Two runtime dependencies vs a component library?
**Answer:** Six screens, a 60 KB gzip bundle and nothing to audit; hash routing and a 25-line
`useLoad` covered routing and fetching. A larger app with forms and tables would justify a router,
a query cache and a component kit.

### Q22. Polling vs push for document indexing status?
**Answer:** Polling every 2 s and only while something is `pending` or `processing`. Indexing takes
seconds; a push channel would add server state for little gain.
Pointer: `web/src/screens/Documents.tsx`.

---

## Justification

### Q23. "Why build a UI at all for an agent backend?"
**Answer:** §24 asks for it, and it paid off: driving the system as a user found five bugs that 373
backend tests and a strict type-check didn't, including a backend filter (the operator run list).
And the approval diff is a safety control the CLI doesn't have.

### Q24. "Why no end-to-end browser tests?"
**Answer:** Time; the end-to-end check was manual in Chrome plus `scripts/demo.sh` for the API path.
It's listed as a limitation; Playwright against a fake-LLM stack in CI is the next step.

---

## System design

### Q25. Walk through approving a write from the UI.
**Answer:** Chat streams a run; the graph's `select_or_execute_tools` asks for `workspace_write`,
the harness records a pending approval and the graph interrupts with a Postgres checkpoint; the
stream ends `awaiting_approval` and Chat links to Approvals. Approvals lists it, reads the current
file and shows the diff. Approve → `POST /approvals/{id}/approve` → conditional UPDATE claims it
once → the graph resumes from the checkpoint, the harness executes the write with an idempotency
receipt, verify and finalize run, and the response carries the answer with a `[T1]` reference to
the receipt.
Pointer: `web/src/screens/Approvals.tsx`; `app/agents/graph.py:resume`.

### Q26. What would change for a hosted, multi-user UI?
**Answer:** Real auth (OIDC, httpOnly cookies, CSRF, CSP), generated API types, resumable streams
with event ids and runs that survive disconnects, server-side approval previews, and Playwright in
CI.
Pointer: journal 13 §9.
