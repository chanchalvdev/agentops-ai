# Journal 13 — The React Web UI (Day 5, M13)

Milestone: the main part of Day 5 in product.txt §18 — "Web UI (Section 24) over the finished
API" — implementing the six §24 screens (Chat, Documents, Workspace, Approvals, Executions,
Evaluations & Monitoring) with the §24 rule "the UI must not hold credentials beyond the user's
API token and must never bypass approvals". Branch `day-5-ui-demo`, code in commit `cff28ed`
(14 Nov). Written by the Recorder agent from `web/`, `reports/ui-api.log`, `reports/ui-vite.log`
and test/build output; what happened in the browser session is reported by the build session and
marked as such where no file shows it.

Status (re-run by the Recorder on 14 Nov):
- `npx tsc --noEmit` (strict, `noUncheckedIndexedAccess`) → clean.
- `npx vitest run` → **6 passed** (1 file).
- Production build present in `web/dist`: one JS bundle of **193,683 bytes**, **60,095 bytes
  gzipped** (`gzip -9`), plus 19 KB of CSS. The Recorder measured these from `dist/`; it did not
  rebuild.
- Backend: 373 passed (`-W error`), ruff, mypy strict (journal 12).

Not in this milestone: a login of its own (the API key is the only credential), SSE reconnect, a
PTY terminal, a mobile layout, end-to-end browser tests in CI.

---

## 1. What was built

### Stack and shape (`web/`)
- **React 18.3.1 + TypeScript 5.6 (strict) + Vite 5.4 + Tailwind 3.4.** Runtime dependencies:
  `react` and `react-dom` only. ~2,400 lines in `src/`: six screens, a small component kit
  (`components/ui.tsx`: `Card`, `Badge`, `Button`, `Drawer`, `ErrorBox`, `useLoad`), and `lib/`
  (`api.ts`, `sse.ts`, `diff.ts`, `format.ts`, `types.ts`).
- **Routing by URL hash** (`#/executions/<id>`) — no router dependency; deep links work.
- **`main.tsx` renders under `<StrictMode>`**, which double-mounts effects in development. That
  surfaced a real bug (§4.2).

### No CORS: Vite proxies the API (`web/vite.config.ts`)
- In `dev` and `preview`, `/api` (with `ws: true` for the terminal WebSocket), `/health` and
  `/ready` are proxied to `AGENTOPS_API_URL` (default `http://localhost:8000`). The browser sees
  one origin, so the API needed no CORS configuration — and no CORS change that could widen who
  may call it.
- `AGENTOPS_API_URL=http://localhost:8010 npm run dev` was used all session: port 8000 was taken by
  another project's server (build session; `reports/ui-api.log` shows `Uvicorn running on
  http://127.0.0.1:8010`).

### The thin client (`web/src/lib/api.ts`)
- Header comment: "No business rules live here: every decision (permissions, approvals, limits,
  tenant isolation) is the server's; the UI only sends the user's API key."
- The key lives in `localStorage` under `agentops.apiKey` (wrapped in `try`; private mode falls
  back to page lifetime) and is sent as `X-API-Key`. Changing the key remounts the screen
  (`key={apiKey:screen}` on `<main>`), so nothing from the previous tenant survives on screen.
- Errors are parsed from the API's envelope `{"error": {code, message, request_id}}` into
  `ApiError`, including `Retry-After` for 429.
- The sidebar shows `/ready` and the tenant id (from `GET /api/v1/metrics`, which is filtered to the
  caller and so names the tenant).

### SSE over POST (`web/src/lib/sse.ts`, `streamAgent`)
- `POST /api/v1/agent/run/stream` takes a JSON body and an API-key header. **`EventSource` only
  does GET and cannot set headers**, so the stream is read with `fetch` →
  `body.pipeThrough(new TextDecoderStream())` → `SseParser.push(chunk)`.
- `SseParser` is incremental: it normalises `\r\n`/`\r`, splits on blank lines, drops comment lines
  (the server's `: keep-alive` from the Day 2 fix `b497479`), joins multi-line `data:`, defaults the
  event to `message`, and keeps a partial block buffered across network chunks.
- **Stop** aborts the fetch (`AbortController`). On the server that is a client disconnect, which
  Day 2 turned into a persisted `cancelled` execution (interview 05 Q21).

### Screens (`web/src/screens/`)
| Screen | What it does | API |
|---|---|---|
| Chat (282 lines) | Example questions; graph/loop toggle; live step timeline from `step`/`model_call`/`tool_call`/`correction`/`approval` events; answer with `[S#]` links that open the **source chunk drawer** and `[T#]` links that show the tool result; counters and tokens; "Review in Approvals" when paused | `/agent/run/stream`, chunk endpoint (M12) |
| Documents (149) | Upload with optional JSON metadata, poll every 2 s while anything is `pending`/`processing`, reindex, delete; says "already uploaded (identical content): not indexed again" for duplicates (fixed in M14) | `/documents` |
| Workspace (257) | Lazy file tree, viewer/editor (the user's own writes go through `PUT /workspace/file`), terminal over the sandbox WebSocket with refusals in red, audit log | `/workspace/*`, `/workspace/terminal` |
| Approvals (213) | Pending writes/edits/exec with a **diff preview against the current file**; approve, or reject with a reason; shows the resumed run's answer | `/approvals`, `/workspace/file` |
| Executions (133) | Recent runs (M12) and a per-run ledger timeline with latency bars scaled to the slowest event, tokens, cache hits | `/executions`, `/executions/{id}/trace` |
| Evaluations & Monitoring (309) | Runs with category/evaluator scores and failing cases; compare two runs through the regression gate; monitoring summary, drift signals, alerts, "Run check now"; a 403 becomes an explanation | `/evaluations/*`, `/monitoring/*` |

### The approval diff (`web/src/lib/diff.ts`, `Approvals.tsx:Preview`)
- `lineDiff` is an LCS line diff; `withContext` keeps 3 lines around changes and collapses the
  rest into "N unchanged lines".
- For `workspace_write` the "after" is the proposed content; for a new file the "before" is empty,
  so every line is an addition. For `workspace_edit`, `applyEdit` replaces the **first** occurrence
  of `old` with `new` — mirroring the tool only to preview it. If `old` is not in the file the
  preview says **"the tool will refuse this edit"**. Files over the read limit get a note that the
  diff covers the first part only.
- The preview is display only. Approve/reject is a POST to the API; the server decides and applies.

### Terminal (`Workspace.tsx`)
- `openTerminal()` connects to `/api/v1/workspace/terminal` and sends `{"api_key": …}` as the first
  frame (browsers can't set WebSocket headers; Day 3 designed the protocol for this, interview 06
  Q39). Close codes are explained: 4401 "unauthenticated", 4403 "this tenant has no workspace
  permission".
- One command at a time (`busy` until `exit` or `error`); no PTY.

### Resilience
- `ScreenBoundary` (`App.tsx`): an error boundary around the active screen — "One broken screen
  must not blank the whole app: show the error and keep the navigation."
- `useLoad`: a request counter so only the latest response is applied (a slow earlier request can't
  overwrite a newer one).

### CI (`.github/workflows/ci.yml`, job `web`)
- Node 22, `npm ci` with the lockfile cache, then `npm run typecheck`, `npm test`, `npm run build`.

---

## 2. Files touched

New: `web/` — `package.json`, `package-lock.json`, `index.html`, `vite.config.ts`,
`tsconfig.json`, `tailwind.config.js`, `postcss.config.js`, `.gitignore` (`node_modules/`,
`dist/`), `src/main.tsx`, `src/App.tsx`, `src/index.css`, `src/components/ui.tsx`,
`src/lib/{api,sse,diff,format,nav,types}.ts`, `src/lib/lib.test.ts`,
`src/screens/{Chat,Documents,Workspace,Approvals,Executions,Evaluations}.tsx`.

Modified: `.github/workflows/ci.yml` (`web` job), `README.md` (Web UI section, troubleshooting,
known limitations), plus the M12 API changes.

Not in git: `web/node_modules`, `web/dist`, `reports/ui-api.log`, `reports/ui-vite.log`.

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Plain React + Tailwind, two runtime deps | A component library, a router, a data-fetching library | Six screens; a 60 KB gzip bundle; nothing to audit or upgrade. Hash routing and `useLoad` cover what was needed. |
| `fetch` + own SSE parser | `EventSource`; a library | `EventSource` is GET-only with no custom headers; the stream is a POST with an API key. The parser is 36 lines and unit-tested. |
| Vite proxy, same origin | CORS on the API | No server change; nothing widens who can call the API from a browser. |
| API key in `localStorage` | Cookie session; key in memory only | §24 allows only the user's API token. A cookie would need a login flow and CSRF protection; memory-only loses the key on reload. Risk accepted and documented: any XSS can read it. |
| Diff computed in the browser, display only | A server "preview" endpoint | The arguments and the current file are already readable by the user; the server still applies the change. A server preview would be more exact (§8). |
| `applyEdit` mirrors "first occurrence" | Show only the raw `old`/`new` strings | The reviewer needs to see the result in context — that is what exposed the duplicated line (§4.1). |
| Per-screen error boundary | One app-level boundary | A crash in one screen keeps navigation and the other screens alive. |
| 403 → explanation | Generic error page | A non-operator reaching Evaluations is a normal case, not an error. |
| Polling for document status (2 s, only while busy) | WebSocket/SSE for indexing | Indexing takes seconds; polling stops when nothing is in flight. |

---

## 4. Driven end to end in Chrome against the live API (build session)

The build session drove every screen in Chrome against the live API on port 8010 with qwen3:8b /
qwen3:1.7b on Ollama. `reports/ui-api.log` holds only the last ~35 minutes of API logs (from
20:35 UTC on 13 Nov; 00:35 local on 14 Nov), so most of what follows is the session's report; the
log confirms the parts noted.

### 4.1 What was exercised
- **Chat**: a cited answer; clicking `[S1]` opened the source chunk drawer (M12 endpoint).
- **A bad edit, caught by the diff.** Asked to add a function, the agent proposed a
  `workspace_edit` to a file that already had the function. The diff preview showed the edit
  would **duplicate a `return` line**. It was rejected with a reason. This is the Day 3
  `ws-edit-approved` behaviour (the model re-proposing an edit that already applied) seen from the
  reviewer's side, and the reason the diff exists.
- **The resumed run then failed with `provider_unavailable`.** The log shows it: a
  `POST /api/v1/approvals/916d4b6f…/reject` → 200 after 5.4 s, preceded by `graph node stopped …
  node: verify_answer, code: provider_unavailable`. Cause (build session): macOS had **killed the
  Ollama server under memory pressure** (swap around 10 GB). Ollama was restarted; the README
  Troubleshooting now lists the symptom (`ollama ps` shows nothing, the process is gone).
- **A new-file write approved and written** (`POST /api/v1/approvals/fea216ba…/approve` → 200 in
  the log); the file appeared in the Workspace tree.
- **Terminal refusals**: `rm -rf calc` → `command_not_allowed`; `cat ../../.env` →
  `workspace_access_denied`; a pipe → `invalid_request` (no shell).
- **Evaluations**: comparing the committed Day 4 baseline and optimized runs → **87% → 95%, no
  breaches**, three newly passing tool-use cases.
- **Monitoring**: summary and "Run check now".
- **Tenant B** (`dev-key-tenant-b`): evaluations and monitoring explained as operator-only (403,
  `permission_denied` ×5 in the log), workspace refused (403, terminal closed with 4403), tenant A's
  executions and documents invisible.

### 4.2 Bugs found by driving it, and fixed
| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | Workspace screen blank; console "destroy is not a function" | The terminal effect returned something that was not a cleanup function (build session); React called it on unmount and the whole tree unmounted | Explicit effects that return a cleanup function, and `ScreenBoundary` so one screen's crash can't blank the app |
| 2 | "Terminal closed (1006)" printed on every visit | StrictMode mounts, unmounts and remounts in dev; the first socket's `onclose` fired into the new session's log | Cleanup detaches `onmessage`/`onclose` **before** `socket.close()` ("a socket closed by this cleanup must not report into the next session") |
| 3 | Token total wrong in Chat | It read `usage.total_tokens`, which is a Python `@property` on `Usage` (`app/llm/schemas.py:39`) and is **not serialized** by Pydantic | Sum `input_tokens + output_tokens` in the UI |
| 4 | Operational stats on Monitoring empty | The UI read the wrong (nested) keys of the summary | Types aligned with `operational.latency_ms.p95`, `operational.tokens.per_execution` |
| 5 | Operator run list empty | Runs from the CLI are `requested_by "cli"`; the API filtered by tenant | API change + regression test (journal 12 §4.2) |

`reports/ui-vite.log` shows the fixes landing as hot updates (`App.tsx`, `Workspace.tsx`,
`Evaluations.tsx`, `types.ts` between 00:36 and 00:44 local) and a final pass over every screen at
01:08 (the duplicate-upload message, journal 14).

---

## 5. How it was verified

- `src/lib/lib.test.ts` (vitest, 6 tests): SSE events split across chunk boundaries with a
  keep-alive comment and `\r\n` line endings; multi-line `data` and the default event name; line
  diff add/remove; new file = all additions and long unchanged runs collapse to a gap;
  `applyEdit` returns `null` when the old text is absent; `splitCitations` splits `[S1]`/`[T1]`.
- `tsc --noEmit` with `strict` and `noUncheckedIndexedAccess`.
- Production build (`tsc --noEmit && vite build`).
- The CI `web` job runs all three.
- Manual end-to-end in Chrome (§4). **No automated browser test** exists.

---

## 6. Commands

```bash
cd web
npm install
AGENTOPS_API_URL=http://localhost:8010 npm run dev    # API on 8010 when 8000 is taken
npm test && npm run typecheck && npm run build
npm run preview                                       # serves dist/ on 4173 with the same proxy
```

---

## 7. What this milestone taught

1. **The browser API decides the protocol details.** SSE over POST rules out `EventSource`;
   WebSockets can't carry headers. Both were anticipated by the Day 2/3 API design (keep-alive
   comments, key in the first frame), and the UI only had to honour them.
2. **A diff is a safety feature, not a nicety.** The one bad edit of the session was stopped
   because the reviewer could see a duplicated line, not because any check on the server caught it.
3. **Driving the real thing finds what tests don't.** Five bugs, none caught by type-checking or
   unit tests: an effect cleanup, StrictMode, a non-serialized Python property, nested keys, and a
   backend filter that only real data could expose.
4. **Python and TypeScript types drift at the boundary.** `total_tokens` exists in Python and not
   in JSON; the UI's `types.ts` is hand-written and was wrong twice. Generating types from OpenAPI
   would remove that class of bug.
5. **The machine is part of the system.** The UI session's only backend failure was the OS killing
   the model server; the API reported it as `provider_unavailable`, which is the right status.

---

## 8. Known limitations and open findings (Recorder review)

1. **API key in `localStorage`**: readable by any script on the origin. There is no
   Content-Security-Policy and no session expiry. Acceptable for a local tool; not for a hosted one.
2. **No SSE reconnect.** A dropped connection cancels the run (Day 2 design); the UI shows the
   error and the run can be read from Executions afterwards.
3. **The approval preview is a re-implementation.** `applyEdit` mirrors the tool's "first
   occurrence" rule; if the tool's semantics change, the preview can lie. It also diffs against the
   file *now*, which may differ from the file when the tool runs. A server-side preview would fix
   both.
4. **Hand-written `types.ts`** (286 lines) drifted from the API twice in one session; generate from
   `/openapi.json`.
5. **One terminal command at a time, no PTY**; the terminal WebSocket is still not rate limited
   (journal 11).
6. **No mobile layout**; the sidebar is fixed width.
7. **No automated browser tests**; the end-to-end check is manual (and `scripts/demo.sh`, journal 14,
   for the API path).
8. **`reports/ui-api.log` covers only the end of the session**, so most §4 claims rest on the build
   session's report.

---

## 9. What I'd do differently at 100x scale

- **Real auth** for a hosted UI: OIDC login, short-lived tokens in httpOnly cookies, CSRF
  protection, CSP; API keys stay for machines.
- **Generated client and types** from OpenAPI, checked in CI.
- **Resumable streams**: event ids and `Last-Event-ID`, with runs that continue after a disconnect
  (a durable run queue), so the UI reconnects instead of cancelling.
- **Server-side approval previews** (the tool's own dry run) so the diff is exactly what will be
  applied.
- **Playwright** end-to-end tests against a stack with a fake LLM in CI.
