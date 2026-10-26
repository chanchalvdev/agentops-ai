# Journal 06 — Sandboxed Workspace Tools, Workspace API, Terminal (Day 3, M6)

Milestone: first part of Day 3 in product.txt §18 — the §9.1 sandboxed workspace (path jail,
command allowlist, scrubbed executor, audit), the six `workspace_*` agent tools behind the
approval gate, the §14 workspace endpoints and terminal WebSocket, per-tenant workspace
permissions, and `agentops approvals / approve / reject` in the CLI. Branch
`day-3-workspace-observability-eval`, **uncommitted** working tree on top of `b497479`.
Written by the Recorder agent from the code, the test output, and facts reported from the build
session (marked as such), not from the plan.

Status: **unit + security = 227 passed** (re-run by the Recorder: `uv run pytest -q -W error
tests/unit tests/security` → `227 passed in 2.55s`). The full suite with Postgres was **not
re-run after the last fix** — see §5. 59 new tests (44 security, 9 unit, 6 integration); the
suite collects 289 non-live tests (230 before M6 + 59).

**Update after the Recorder review:** seven findings were fixed in code (§10); DB-free suite now
**243 passed** (re-run by the Recorder); workspace test files 47 + 10 + 6 = 63; integration still
not re-run (DB down). §1-§9 describe the tree as reviewed; §10 lists what changed since.

Not in this milestone: the Langfuse adapter, golden dataset and evaluators (rest of Day 3 — an
`app/observability/` package appeared in the working tree while this was being written; it is not
covered here), an OS-level sandbox, UI.

Line numbers refer to the working tree at the time of writing; function names are the stable
reference.

---

## 1. What was built, by subsystem

### Path jail and file operations (`app/workspace/jail.py`)
`WorkspaceManager(root, audit, max_read_bytes, max_write_bytes, exec_timeout_s,
max_output_bytes)` (472-489) owns the base directory; `for_tenant(tenant_id)` returns a
`Workspace` rooted at `<root>/<tenant_id>` (created on demand, `realpath`'d once).

- **Tenant id** must match `^[A-Za-z0-9_-]{1,64}$` (46, checked in `Workspace.__init__` 149-160),
  so `../tenant_a` or `.receipts` can never be a tenant. Receipts live in `<root>/.receipts/<t>` and
  command `HOME` in `<root>/.home/<t>`: outside every tenant root, so the agent cannot read or
  forge them.
- **`Workspace.resolve(path, write)`** (164-179), the single gate for every path:
  1. NUL or > 1000 chars → reject;
  2. absolute or `~…` → reject; any `..` component → reject (before touching the disk);
  3. `os.path.realpath(root / path)` — **symlinks are followed before the containment check**, so
     a link pointing outside is caught;
  4. the result must equal the root or start with `root + os.sep` (the `+ sep` stops
     `/ws/tenant_a_evil` matching `/ws/tenant_a`);
  5. `denied_reason` is applied to **both** the requested path and the resolved relative path, so a
     harmless-looking `config.txt → .env` link is refused.
- **`denied_reason(rel, write)`** (56-72): any component named `.ssh .env .netrc .git-credentials
  .pypirc .npmrc .aws .gnupg`, starting with `.env. id_rsa id_ecdsa id_ed25519`, or ending in
  `.pem .key .p12 .pfx` → "sensitive file". `.git/config` is sensitive for reads; **anything under
  `.git` is read-only** for writes (hooks/config there would run code). Note `.env.example` is
  denied by the `.env.` prefix rule (accepted, see §9).
- **`list_dir`** (238-264): sorted dirs-first, skips denied entries silently ("do not even reveal
  that secrets exist"), reports symlinks as `symlink` without following them, cap 500 entries.
- **`read`** (266-281): `os.open(full, O_RDONLY | O_NOFOLLOW)`, reads `limit + 1` bytes to detect
  truncation, `limit = min(requested, max_read_bytes)`. `_decode` (123-131) rejects NUL in the
  first 8 KiB (binary) and non-UTF-8, except a cut inside a multi-byte character at the truncation
  point, which is trimmed.
- **`search`** (283-342): **literal** substring search, like `grep -rF` — no regex. `os.walk(...,
  followlinks=False)`, skips `.git node_modules __pycache__ .venv .pytest_cache` and denied names,
  skips symlinked files, filters with `PurePosixPath.full_match(glob)`, caps 2000 files / 1 MB per
  file / `max_results`, lines cut to 300 chars.
- **`write`** (388-393) and **`edit`** (395-418) both resolve with `write=True`, then
  `_write_atomic` (350-378): temp file in the same directory (`mkstemp(prefix=".agentops-")`),
  write + `flush` + `fsync`, `os.replace`; temp removed on any failure. `_check_text` (380-386):
  UTF-8 size ≤ `max_write_bytes`, no NUL. `edit` replaces **exactly one** occurrence of `old`:
  0 → "old text not found", n > 1 → "found n times; add context".
- **Idempotency receipts.** `_receipt_path(actor)` (344-348): only for `actor.kind == "agent"`
  with both `execution_id` and `call_id`; key = `sha256(f"{execution_id}:{call_id}")`. If the
  receipt exists, `_write_atomic` returns the stored `WriteResult` with `replayed=True` and writes
  nothing; `edit` checks the receipt before reading the file (402-403). Closes the Day 2 deferred
  item "approved write replayed on recovery (no idempotency key)" — with a crash window (§9 item 2).
- **Audit wrapper** `_audited` (186-206): runs the blocking function in `asyncio.to_thread`, maps
  `OSError` to typed errors (`_map_os_error` 134-143: not found → 404, is-a-directory → 422,
  permission → 403), records `ok` / `rejected` (any `WorkspaceAccessError`) / `error`, then returns
  or re-raises.
- **Commands** `plan(argv)` (422-424) validates only; `stream(argv, actor)` (426-461) plans, runs
  `run_streaming` with `cwd=root` and `scrubbed_env(home, root.parent)`, and audits in a `finally`
  (`exec_interrupted` if no `ExecResult` was produced); `exec` (463-469) drains `stream`.

### Command executor (`app/workspace/executor.py`)
- **No shell, ever.** `asyncio.create_subprocess_exec(*exec_argv, stdin=DEVNULL,
  stderr=STDOUT, start_new_session=True)` (270-278). Pipes, globbing, redirection and `$(…)` do not
  exist; `$(touch …)` is a literal filename (tested).
- **`RULES`** (72-136), one `CommandRule` per command with its *allowed options*:

  | Read-only (`writes=False`) | Write / execute (`writes=True`, agent needs approval) |
  |---|---|
  | `ls` `cat` `head` `tail` `wc` `grep` `find` `git status` `git diff` `git log` | `python -m pytest`, `pytest`, `git add`, `mkdir` |

  Options are where "read-only" programs hide writes and escapes (`grep -f /etc/passwd`,
  `find -delete/-exec`, `git diff --output=FILE`, `ls --color=always`), so every option not listed
  is rejected. `find` has `combined_short=False` (its `-name` is not `-n -a -m -e`); `head/tail/git
  log` accept `-5`; `grep`'s first positional is the pattern, not a path.
- **`plan_command(argv, resolve)`** (173-229): 1-32 args, each non-empty, no NUL, ≤ 1000 chars;
  rule match by argv prefix, longest prefix wins (`_match` 160-165; no match → the error lists
  every allowed command); option parsing with `--`, `--key=value`, combined short flags; option values
  ≤ 200 chars; **every positional path goes through the same `Workspace.resolve`** (`mkdir`
  positionals with `write=True`). Programs are resolved with `shutil.which(program,
  path=SAFE_PATH)`; `python` is the server's own `sys.executable`. Returns a `CommandPlan` whose
  `writes` decides approval.
- **Hardening injected before user args:** git always runs as `git -c core.fsmonitor=false -c
  core.hooksPath=/dev/null …` (33); `git diff/log` add `--no-ext-diff --no-textconv --no-color`;
  pytest adds `-p no:cacheprovider --rootdir=. -c /dev/null` (68) so no config above the
  workspace is picked up and no cache dir is written.
- **`scrubbed_env(home, ceiling)`** (232-250): the *complete* environment — `PATH=SAFE_PATH`,
  per-tenant `HOME`, `LANG`, `TERM=dumb`, `NO_COLOR`, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, `GIT_PAGER=cat`,
  `GIT_CEILING_DIRECTORIES=<root>` (git cannot walk up into an enclosing repository),
  `PYTHONDONTWRITEBYTECODE`, `PYTHONNOUSERSITE`. Nothing is inherited, so `AGENTOPS_API_KEYS`, DB
  URLs and cloud credentials never reach a command.
- **`run_streaming`** (258-316): yields decoded text chunks as they arrive (incremental UTF-8
  decoder with `replace`), then one `ExecResult{argv, exit_code, output, truncated, timed_out,
  duration_ms}`. `asyncio.timeout(timeout_s)`; output cap `max_output_bytes`; on timeout, cap, or
  the consumer closing the iterator, `_kill` sends `SIGKILL` to the **process group** (253-255:
  pytest may spawn children). `exit_code=None` means "killed".

### Audit (`app/workspace/audit.py`, migration `0003_workspace_audit.py`)
`AuditEntry{tenant_id, actor: agent|user, execution_id, key_id, operation:
list|read|search|write|edit|exec, target (path or command line, never contents, ≤ 2000),
status: ok|rejected|error, error_code, exit_code, bytes, duration_ms}`. `AuditSink` protocol with
`MemoryAudit` (tests) and `DbAudit` (one transaction per row; `list(tenant, limit)` newest first).
Table `workspace_audit` (`app/db/models.py:WorkspaceAuditEvent`), index `(tenant_id, id)`,
**no FK to `agent_executions`** on purpose — the audit trail outlives deleted executions.

### Agent tools (`app/tools/workspace.py`)
Thin adapters: the tenant comes from `ctx.tenant_id`, never from the model (`_ws`, 38-39); the
actor is `Actor("agent", execution_id=ctx.execution_id, call_id=ctx.current_call_id)` (41-43).

| Tool | Permission | Approval | Ledger arguments / result summary |
|---|---|---|---|
| `workspace_list(path)` | `workspace:read` | no | default / `{entries: n}` |
| `workspace_read(path, max_bytes 100-20000)` | `workspace:read` | no | default / `{path, size, truncated}` |
| `workspace_search(query, glob, case_sensitive, max_results ≤ 100)` | `workspace:read` | no | default / `{matches, files_scanned}` |
| `workspace_write(path, content ≤ 200k)` | `workspace:write` | **always** | `{path, chars, sha256[:16]}` / `{path, bytes, sha256, created, replayed}` |
| `workspace_edit(path, old, new)` | `workspace:write` | **always** | `{path, old_chars, new_chars, old_sha256}` / `{path, bytes, sha256, replayed}` |
| `workspace_exec(argv)` | `workspace:exec` | **per command** (`approval_needed` → `plan(argv).writes`) | default / `{exit_code, timed_out, truncated, duration_ms}` |

Write tools are `idempotent = False` → the harness never retries them. `workspace_exec.timeout_s
= 610` sits above the max exec timeout (600) so the executor's own timeout (which kills the group
and returns a result) fires first. The model sees at most 6000 chars of command output
(`_AGENT_OUTPUT_CHARS`, 27).

### New `Tool` hooks (`app/tools/base.py`)
- `approval_needed(args, ctx) -> bool` (32-36): default `requires_approval`. `requires_approval`
  now means "**may** need approval" — the custom loop drops such tools; a tool may relax it per
  call. Raising an `AppError` here rejects the call before anyone is asked.
- `ledger_arguments(args)` (38-41): what the ledger stores; default the full dump.
- `ledger_output(output)` (43-45): a small summary added to the `tool_call` event as `result`;
  default `{}`. `DocumentSearch` now records `chunk_ids`, `document_ids`, `filenames`.

### Harness changes (`app/agents/harness.py`)
- `call_signature(call)` (46-48) moved here from the loop: `name:json.dumps(args, sort_keys=True)`.
  Shared by the loop's repeat detection and the graph's duplicate guard.
- `call_tool` (221-315): after `authorize_tool_call`, `tool.approval_needed(args, ctx)` is called
  inside `try`; an `AppError` becomes a normal rejection (`_reject`, ledger `tool_call/rejected`) —
  so `bash -c …` is refused with `command_not_allowed` and **no approval is ever created**.
  The pending `approval` event stores `tool.ledger_arguments(args)` (hashes, not file contents).
- `ctx.current_call_id = call.id` around `tool.run` (252, cleared in `finally` 271): the write
  tools' idempotency key.
- The success `tool_call` event carries `result: ledger_output(...)` when non-empty.

### Graph changes (`app/agents/nodes.py`, `state.py`; `PROMPT_VERSION = "agent-graph-v2"`)
- **Intent `workspace_task`** in `CLASSIFY_PROMPT`; `classify_request` (164-178) sets
  `needs_retrieval = False` and `needs_tools = True` for it, so `plan_workflow` returns `[]`
  without a model call and the graph goes straight to `select_or_execute_tools`.
- **Multi-round tools.** `TOOLS_PROMPT` now shows "tool results so far" and asks for the next
  calls. New state: `tool_rounds` (model-planned rounds), `round_executed` (calls executed in the
  latest pass; a failed call counts, so the model can correct a bad path), `completed_calls`
  (signatures of successful calls). `after_tools` (487-498) loops back to
  `select_or_execute_tools` only when `intent == "workspace_task"`, `round_executed > 0` and
  `tool_rounds < MAX_TOOL_ROUNDS = 4` (29). Resume passes (executing approved calls) do not
  increment `tool_rounds`. A rejected call adds a `rejected_by_reviewer` result but is not
  "executed", so a rejection alone ends the tool loop.
- **Duplicate guard** (259-268): a `queued` call whose signature is in `completed_calls` is
  refused **before** `call_tool` — ledger `tool_call/rejected error_code=duplicate_call`, and the
  model gets a `{"error": "duplicate_call"}` tool result. No approval is created. Born from live
  run 2 (§4).
- **`_draft_schema(store)`** (151-158): `GroundedDraft`'s JSON schema with
  `citations.items = {"type": "string", "enum": <labels shown>}`; used by `generate_answer`
  (363). Born from live run 1 (§4).

### Permissions (`app/core/security.py`, `app/agents/service.py`)
- `DOCUMENT_PERMISSIONS = {documents:read}`, `WORKSPACE_PERMISSIONS = {workspace:read,
  workspace:write, workspace:exec}` (20-21). `Principal` gains `permissions`.
- `ApiKeyAuthenticator.permissions_for(tenant)` (57-61): tenants in
  `settings.workspace_tenant_ids` get documents + all three workspace permissions; others get
  documents only. "Permissions come from server configuration, never from the request."
- `AgentService.new_context(..., permissions)` (54-80): with no explicit `tools` list, the model is
  **offered only tools whose `permissions <= ctx.permissions`**; with an explicit list, unknown
  names are still 422 and every call is still permission-checked by the harness. The API routes
  pass `principal.permissions`; the CLI passes `services.auth.permissions_for(tenant)`.
  Permissions are saved in the checkpoint snapshot and restored on resume.
- Closes the Day 2 deferred item "approval flow unreachable in production" (journal 05 §8 item 1).

### Workspace HTTP API and terminal (`app/api/workspace.py`, prefix `/api/v1/workspace`)

| Method + path | Permission | Notes |
|---|---|---|
| `GET /tree?path=` | `workspace:read` | `ListResult` |
| `GET /file?path=&max_bytes=` | `workspace:read` | `FileContent`, server cap applies |
| `PUT /file` `{path, content}` | `workspace:write` | `WriteResult`; body ≤ 5 MB, configured cap on top |
| `GET /search?q=&glob=&case_sensitive=` | `workspace:read` | extra vs §14 |
| `POST /exec` `{argv}` or `{line}` | `workspace:read` for read-only commands, `workspace:exec` for write commands (`_exec_permission` 81-83) | `ExecResult`; **no approval** — the user is the human |
| `GET /audit?limit=` | `workspace:read` | tenant's `workspace_audit`, newest first; extra vs §14 |
| `WS /terminal` | `workspace:read` to connect | see below |

`ExecRequest` (49-78): exactly one of `argv` / `line`. `line` is split with
`shlex.shlex(posix=True, punctuation_chars=True, whitespace_split=True)`; any token made only of
`();<>|&` → 422 "Pipes, redirects, ; and && are not supported (no shell)". `$` is not checked —
there is no shell to substitute.

Terminal protocol (`terminal`, 147-199), JSON frames, **not a PTY**: auth by `X-API-Key` header
or a first frame `{"api_key": …}` within 10 s (browsers cannot set headers on a WebSocket);
close `4401` unauthenticated, `4403` no `workspace:read`; then `{"type":"ready","tenant"}`;
per message `{"line"|"argv"}` → `output` frames as the process writes, then
`{"type":"exit", ...ExecResult}` or `{"type":"error", code, message}`. One command at a time,
through `Workspace.stream` (same executor, same audit).

### CLI (`app/cli.py`)
- `agentops run` passes `permissions_for(tenant)`; on `awaiting_approval` it prints
  `approval <id>: <tool> <args>` for this execution.
- `agentops approvals [--status pending|approved|rejected|all]` (174), `agentops approve <id>
  [--reason]` (198), `agentops reject <id> [--reason]` (204) → `AgentService.decide`, i.e. the same
  conditional-UPDATE claim and checkpoint restore as the API. Closes journal 05 §8 item 12.

### Settings (`app/core/config.py`)
`workspace_root = Path("workspace")`, `workspace_tenants = ""` (comma list; **empty in `dev` =
`tenant_a`**, in any other env = nobody), `workspace_max_read_bytes` 20 000 (1k-1M),
`workspace_max_write_bytes` 200 000 (1k-5M), `workspace_exec_timeout_s` 60 (≤ 600),
`workspace_max_output_bytes` 64 000. The `_check` validator refuses a root that resolves to `/`,
the home directory, or a directory containing `.git`. `/workspace/` is gitignored.

---

## 2. Files touched

```
app/workspace/{__init__,jail,executor,audit}.py, app/tools/workspace.py, app/api/workspace.py  (new)
app/db/migrations/versions/0003_workspace_audit.py                                             (new)
app/tools/base.py (hooks), app/tools/document_search.py (ledger_output)
app/agents/harness.py, nodes.py, state.py, service.py, custom_loop.py (loop-v2, call_signature)
app/core/security.py, config.py, errors.py, execution_context.py; app/db/models.py
app/services.py (WorkspaceManager, DbAudit, tools), app/main.py (router), app/api/agent.py, app/cli.py
tests/security/test_workspace_security.py (44), tests/unit/test_workspace_agent.py (9),
tests/integration/test_workspace_api.py (6)                                                    (new)
tests/conftest.py (tmp workspace root, workspace_tenants=tenant_a, TRUNCATE workspace_audit)
tests/unit/test_agent_loop.py (PROMPT_VERSION import)
```

New errors (`app/core/errors.py`): `PermissionDeniedError` (403 `permission_denied`),
`WorkspaceAccessError` (403 `workspace_access_denied`), `CommandNotAllowedError(WorkspaceAccessError)`
(`command_not_allowed`). For the agent these are non-terminal tool errors: the model is told and
may adapt.

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **realpath, then containment, then deny-list on both requested and resolved path** | String prefix checks on the raw path; `Path.resolve(strict=…)` only; deny-list on the requested path only | Symlinks are the classic escape; resolving first catches them. Checking the resolved path too catches a link *inside* the root pointing at `.env`. Cost: TOCTOU between check and open (§9). |
| 2 | **Argv + per-command option allowlist**, no shell | `shell=True` with a blocklist; allowlisting program names only | A blocklist never ends; and program-name allowlists miss option escapes (`find -exec`, `grep -f`, `git diff --output`). Cost: the user cannot pipe; every useful option has to be added by hand. |
| 3 | **`pytest --collect-only` is write/exec** | product.txt's read-only list | Collection imports test modules and `conftest.py` — it runs workspace code. Deliberate deviation. |
| 4 | **Literal search** in Python | Regex (`re`), or shelling out to `grep` | A model- or user-supplied regex can backtrack catastrophically, and the search runs in `asyncio.to_thread`, which cannot be interrupted by the tool timeout. Deviation from "pattern" in §9.1. |
| 5 | **Scrubbed env built from nothing** | Copy `os.environ` and delete known secrets | An allowlist cannot forget a secret; a denylist always does. |
| 6 | **Approval decided per call** (`approval_needed`) and **policy errors before approval** | Approve every `workspace_exec`; ask the human about anything | Read-only commands do not need a human; a disallowed command should never reach a human (they might click approve). |
| 7 | **Idempotency receipts keyed by execution id + call id**, outside the tenant root | Rely on the checkpoint; content-hash dedupe | The checkpoint is written after the node, so a crash after the write replays it on resume. A content hash cannot tell "same call replayed" from "user asked twice". Cost: crash window between `os.replace` and the receipt (§9). |
| 8 | **Graph duplicate guard** on successful call signatures | Trust the model; rely on `edit`'s "exactly one occurrence" | Live run 2: an append-style edit keeps `old` in the file, so "exactly one" did not stop a second application. Cost: identical re-reads are also refused (§9). |
| 9 | **Dedicated `workspace_audit` table** + agent calls also in `execution_events` | Only `execution_events` (product.txt) | User/terminal operations have no execution; the table answers "what happened to this workspace?" on its own and survives execution deletion. Deviation. |
| 10 | **User-initiated API/terminal commands need no approval**, but `workspace:exec` for write commands | Route user commands through the approval flow | The user *is* the human; the permission is what separates read from exec. Deviation (product.txt is silent). |
| 11 | **JSON-over-WebSocket terminal, one allowlisted command at a time** | A PTY | §9.1 forbids a raw PTY; a JSON protocol reuses the executor exactly. Cost: no interactive programs, no Ctrl-C (only disconnect). |
| 12 | **First-frame API key** on the WebSocket (header also accepted) | Key in the query string | Browsers cannot set `X-API-Key` on `new WebSocket()`; query strings end up in access logs. |
| 13 | **Ledger stores hashes/sizes for write arguments**; the approval row keeps the full call | Full arguments everywhere; nothing | File contents in traces would leak into every observability backend; the reviewer must still see what they approve (`interrupt` payload → approvals row). |
| 14 | **Citation enum built per call** (`_draft_schema`) | Stronger prompt wording; more corrections | Under constrained decoding an enum makes "sentence in citations" impossible; three prompt-level retries did not help (§4). |
| 15 | **Permissions from server config per tenant**; tools filtered before the model sees them | Per-key scopes; offering all tools and refusing at call time | Smallest change that makes approvals reachable; not offering a tool removes a whole class of wasted calls. Cost: all-or-nothing per tenant (§9). |
| 16 | **Loop mode offers only read-only workspace tools** | Let the loop fail on approval | The loop cannot pause; `requires_approval` tools (incl. all of `workspace_exec`) are dropped there (`custom_loop.py:_run`). |

---

## 4. Live runs (qwen3:8b, graph mode, dev DB) — reported by the build session

Demo workspace `workspace/tenant_a`: `calc/mathlib.py` (`add`, `seconds_to_minutes`),
`calc/test_mathlib.py`, `README.md`.

**Run 1 — "Which functions are defined in calc/mathlib.py in my workspace?"**
- First attempt **failed: `failed_validation` after 3 drafts.** A probe showed qwen3:8b putting
  whole sentences into `citations` (e.g. `"The function add is defined on line 1 [T1]."`) three
  times in a row, despite the field description and the correction feedback.
- Fix: `nodes._draft_schema` — `citations.items` is an enum of the labels actually shown.
- Re-run: **answered, `verification=supported`**: "The functions defined in calc/mathlib.py are
  add(a, b) [T1] and seconds_to_minutes(seconds) [T1]." Path: classify → `workspace_search(query=
  "def", glob="calc/mathlib.py")` → no more tools → answer → verify.

**Run 2 — "In my workspace, add a function minutes_to_seconds(minutes) that returns minutes * 60
to the end of calc/mathlib.py"**
- The model called `workspace_read`, then `workspace_edit(old="    return seconds / 60",
  new=<that line + the new function>)`. The run **paused `awaiting_approval`; the file was
  unchanged.**
- `agentops approve <id>` **in a new process** resumed from the Postgres checkpoint and applied the
  edit correctly (the file on disk now ends with `minutes_to_seconds`).
- **Then the model proposed the identical edit again** (a new approval was created). Approving it
  would have applied it twice: `old` still occurred exactly once after the first edit.
- Fix: the graph duplicate guard (`completed_calls`), regression test
  `test_identical_write_after_success_is_refused_without_approval`.
- That second approval was **left pending in the dev DB** (Docker went down before it could be
  rejected). The guard is verified by the unit test, **not yet by a repeat live run**.

Lessons: (1) field descriptions do not constrain a small model; the schema does — make invalid
output unrepresentable. (2) "Exactly one occurrence" is not idempotency: an append-style edit
leaves its anchor in place. (3) After an approved write, re-asking the model is where repeats come
from; the guard belongs in code, before the human is bothered. (4) The approval path finally ran
end to end across processes, which is what the Day 2 machinery was built for.

---

## 5. How it was verified

**Recorded facts from the build session (not re-derivable by the Recorder):**
- Baseline before M6: **230 passed**.
- During M6 with Postgres up: full suite **287 passed + 2 failing**; both were test-assumption
  issues and were fixed: the bounded-rounds test re-read the *same* file each round, which the new
  duplicate guard correctly refuses (now reads `missing0.md … missing9.md`); and a pending-approval
  count.
- After that fix: unit + security = **227 passed**; `tests/integration/test_workspace_api.py`
  passed **6/6** earlier in the session.
- **Then Docker Desktop was quit (gracefully, twice) and the user chose to leave it off. The final
  full run including integration tests could NOT be repeated after the last fix.** The integration
  suite is therefore unverified on the final tree.
- Dev environment: another project's Postgres held host port 5433, so the agentops DB ran on
  **5435** (`AGENTOPS_DB_PORT=5435`; tests via `AGENTOPS_TEST_ADMIN_DSN` /
  `AGENTOPS_TEST_DATABASE_URL` pointing at 5435).

**Re-run by the Recorder (no DB):** `uv run pytest -q -W error tests/unit tests/security` →
**227 passed in 2.55 s**. `pytest --collect-only` (non-live) → 289 tests (230 + 59).

What the tests prove (§9.1 "Tests must prove"):

| Requirement | Test |
|---|---|
| Path traversal rejected | `test_path_traversal_and_absolute_paths_are_rejected` (`../`, `src/../../`, `/etc/passwd`, `~/.ssh/id_rsa`, NUL); `test_tenants_cannot_reach_each_other`; API: `test_jail_errors_are_clean_api_errors` (403 `workspace_access_denied`) |
| Symlink escape rejected | `test_symlink_escape_is_rejected` (file link, dir link, write through dir link, listing shows `symlink`); `test_symlink_to_a_secret_inside_the_root_is_rejected` |
| Non-allowlisted commands rejected | `test_commands_outside_the_allowlist_are_rejected` (13 cases incl. `git -C /`, `find -exec`, `grep -f`, `git diff --output`, `/bin/ls`); `test_command_path_arguments_go_through_the_jail`; `test_shell_metacharacters_are_plain_arguments`; agent: `test_disallowed_command_is_rejected_before_any_approval` |
| Writes block until approved | `test_edit_waits_for_approval_then_applies` (file unchanged while paused; reviewer sees full `new`; ledger has no `old`); `test_exec_that_runs_code_needs_approval` |
| Rejected approvals perform no write | `test_rejected_write_is_never_performed` |
| Secrets not in the command env | `test_secrets_are_not_in_the_command_environment` (real pytest subprocess asserts `AGENTOPS_API_KEYS`/`AWS_SECRET_ACCESS_KEY` absent) |
| Extras | sensitive names (6 params), `.git` read-only, read caps/binary/missing, atomic capped write, exactly-one edit, receipts (`test_agent_writes_are_idempotent_per_tool_call`), literal search (`(a+)+$` → no match), git ceiling, timeout kills (< 10 s for a 30 s sleep), output cap, audit incl. rejections and no file content, bounded rounds (4), tenant without permission (not offered; refused when forced), loop offers only list/read/search, duplicate guard |
| API / WS (integration, Postgres) | round trip; clean errors; `line` exec; tenant_b 403 `permission_denied` on tree/write/exec; audit rows tenant-scoped incl. `rejected`; terminal: `ready`, `output*`+`exit`, jail and allowlist errors in-band, 4401, 4403 |

---

## 6. Commands

```bash
uv run pytest -q -W error tests/unit tests/security          # 227 passed, no DB needed

AGENTOPS_DB_PORT=5435 docker compose up -d db               # 5433 was taken on this machine
export AGENTOPS_TEST_ADMIN_DSN=postgresql://agentops:agentops@localhost:5435/agentops
export AGENTOPS_TEST_DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5435/agentops_test
uv run alembic upgrade head                                  # 0003_workspace_audit
uv run pytest -m "not live" -W error                         # full suite (not re-run after the last fix)
uv run pytest tests/integration/test_workspace_api.py -v

uv run agentops run "Which functions are defined in calc/mathlib.py in my workspace?" --trace
uv run agentops run "In my workspace, add a function minutes_to_seconds(minutes) ... calc/mathlib.py"
uv run agentops approvals                                    # --status all
uv run agentops approve <approval-id>                        # or: reject <id> --reason "..."

curl -H "X-API-Key: $KEY" 'localhost:8000/api/v1/workspace/tree?path=calc'
curl -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"line":"grep -n def calc/mathlib.py"}' localhost:8000/api/v1/workspace/exec
curl -H "X-API-Key: $KEY" localhost:8000/api/v1/workspace/audit
# Terminal: ws://localhost:8000/api/v1/workspace/terminal, first frame {"api_key": "..."}
```

Settings: `AGENTOPS_WORKSPACE_ROOT`, `AGENTOPS_WORKSPACE_TENANTS`,
`AGENTOPS_WORKSPACE_MAX_READ_BYTES`, `…_MAX_WRITE_BYTES`, `…_EXEC_TIMEOUT_S`,
`…_MAX_OUTPUT_BYTES` (not yet in `.env.example` / README — §9 item 12).

---

## 7. Deliberate deviations from product.txt §9.1 / §14

1. `pytest --collect-only` is **write/exec** (needs approval): collection runs code.
2. `workspace_search` is **literal**, not a regex/pattern (ReDoS in an uninterruptible thread).
3. Audit goes to a **dedicated `workspace_audit` table**; agent calls *also* land in
   `execution_events` via the ledger.
4. **User-initiated** API/terminal commands need **no approval** (the user is the human) but need
   `workspace:exec` for write commands.
5. Terminal is **JSON-over-WebSocket**, one allowlisted command at a time, not a PTY.
6. **First-frame API-key auth** on the WebSocket.
7. Two extra endpoints: `GET /workspace/search`, `GET /workspace/audit`.

---

## 8. Deferred items closed and opened

**Closed (from journal 05 / `docs/day-checklist.md` Day 2):**
- "An approved write that crashes before its checkpoint is replayed on recovery (no idempotency
  key)" → receipts per `(execution_id, call_id)` (partially: crash window, §9 item 2).
- "Approval flow is unreachable in production until a write tool exists and tenants can be granted
  write permissions" → `workspace_write/edit/exec` + `permissions_for`; exercised live (§4 run 2).
- CLI cannot approve (journal 05 §8 item 12) → `agentops approvals/approve/reject`.

**Opened:** everything in §9, plus: reject the stale live approval left pending in the dev DB;
repeat live run 2 to confirm the duplicate guard live.

---

## 9. Known limitations and open findings

As found at review time. Items marked **→ fixed (§10.n)** were changed in code after the review;
the original text is kept so the history is visible. Everything unmarked is still open.

Recorded as known by the build session:
1. **Not an OS sandbox.** An approved `python -m pytest` runs workspace code with the server's
   privileges, using the server's interpreter (`sys.executable`): it can read anything the server
   can (e.g. a `.env` next to the server; the scrubbed environment only protects env vars). Approval is the control; production needs a
   container/VM (no network, read-only rootfs, separate UID).
2. **→ fixed (§10.1)** **Receipt crash window.** The receipt is written after `os.replace` (`_write_atomic` 365-377).
   A crash between them → the replay writes again: harmless for `write` (same content). For `edit`
   the product.txt facts say the replay reports "old text not found" — **true only when `new` does
   not contain `old`.** For an append-style edit (exactly live run 2's shape: `new = old + more`),
   `old` still occurs once and the replay **applies the edit a second time**. The graph duplicate
   guard does not help here (it is in-state, and the crash lost that state too).
3. **TOCTOU** between `realpath` and `open` is only partly mitigated: `O_NOFOLLOW` on the final
   component for `read`/`edit`; `os.replace` replaces a final-component symlink rather than
   following it; a directory component swapped for a symlink after the check is not covered.
   Only a process that can create symlinks in the workspace (e.g. approved pytest) can race it.
4. **→ partly fixed (§10.3)** **Audit is written after the operation and is not transactional
   with it.** If the audit insert
   fails (DB down) after a successful write, `_audited` raises → the agent sees a tool error for a
   write that happened, and may retry under a new call id (receipts and the duplicate guard both
   miss that). If the harness timeout cancels during `asyncio.to_thread`, the thread finishes the
   operation but no audit row is written (`CancelledError` is not caught in `_audited`).
5. `.env.example` (and any `.env.*`) is denied by the `.env.` prefix rule.
6. **Workspace tenants are a comma list in settings** (dev default `tenant_a`), all-or-nothing:
   no read-only tenant, no per-key scopes (still open). Permissions are frozen into the checkpoint,
   so removing a tenant from the list does not stop approving its already-paused writes
   **→ fixed (§10.7)**.
7. **Loop mode offers only list/read/search**; even read-only `workspace_exec` is graph-only,
   because `workspace_exec.requires_approval = True`.

Found by the Recorder in this review (the Recorder does not change code; fixes were made
afterwards by the build session, see §10):

8. **→ fixed for API/terminal (§10.2)** **Command rejections at the policy stage are not in `workspace_audit`.** `POST /exec` and the
   terminal call `_exec_permission` → `ws.plan(argv)` *before* `ws.stream`, so a
   `command_not_allowed` / jail rejection (and a `permission_denied`) raises without an audit row.
   The integration test `test_jail_errors_are_clean_api_errors` sends `rm -rf .` but
   `test_audit_log_records_rejections_and_is_tenant_scoped` only checks a *read* rejection. The
   agent's equivalent (`approval_needed` → `plan`) is in `execution_events` but also not in
   `workspace_audit`. Contradicts "every operation … including rejections" in the module docstrings.
9. **→ fixed for read tools (§10.4)** **The duplicate guard refuses identical reads too.** Any successful call's signature blocks the
   same call later, so "read `calc/mathlib.py` again to check my edit" returns `duplicate_call`
   and the model is left with the pre-edit content. Exempting read-only tools (or keying reads on
   file version) would fix it.
10. **→ fixed (§10.5)** **`grep -R` and symlinks on Linux.** `grep` allows `-R`; GNU grep's `-R` follows *all*
    symbolic links, so on a Linux host a symlink already present in the workspace (e.g. from a
    cloned repo) would let `grep -R x .` read outside the root. On this Mac, `/usr/bin/grep` is BSD
    grep, and the Recorder verified in a scratch directory that neither `-r` nor `-R` follows a
    directory symlink. `workspace_search` is unaffected (`followlinks=False`). Not tested on Linux.
11. **Terminal robustness.** `websocket.receive_json()` in the command loop is outside the `try`,
    so a non-JSON frame after `ready` raises out of the handler and drops the connection
    **→ fixed (§10.6)**. A disconnect during a silent command is only noticed on the next send, so
    the command runs until the exec timeout (default 60 s) — **still open, untested**.
12. Docs/config gaps: no `AGENTOPS_WORKSPACE_*` entries in `.env.example` or README;
    `docs/day-checklist.md` not updated for M6; `app/db/models.py` docstring mentions "evaluation
    datasets and runs" that do not exist yet; `workspace_root` is relative to the process cwd, and
    the validator only rejects a root that *is* a repository, not one inside a repository (the
    default `./workspace` inside this repo passes; `GIT_CEILING_DIRECTORIES` keeps git out of it).
13. Nits: receipts are never cleaned up; the `duplicate_call` ledger event has no `call_id`
    (**→ fixed, §10.4**); a
    `workspace_task` with no tool results ends with "No relevant passages were found in your
    documents." (document wording); `Workspace.__init__` `mkdir`s on every `for_tenant` call.

---

## 10. Fixed after Recorder review

After the review in §9 the build session changed the code; the Recorder re-read each change.
Line numbers are from the updated tree. **DB-free suite after the fixes: `uv run pytest -q -W
error tests/unit tests/security` → 243 passed** (re-run by the Recorder: `243 passed in 2.65s`;
`tests/unit` now also contains M7's `test_observability.py`, so not all of the +16 are M6).
Workspace test files now collect 47 security + 10 unit + 6 integration = 63. **The integration
tests were not re-run after these fixes — the DB is still down.**

| # | Finding (§9) | Fix in code | Test |
|---|---|---|---|
| 10.1 | Receipt crash window: append-style edit applied twice (item 2) | **Write-ahead receipts.** `_write_atomic` (389-406) builds the `WriteResult` (with the *intended* `sha256`) first, writes the receipt via `_replace` (377-387, temp + fsync + `os.replace`), and only then replaces the file. `_replayed(full, actor)` (366-374) returns the stored result only if the file's **current sha256 equals the receipt's**; otherwise the receipt is treated as "intent recorded, write never happened" and the write is redone. `edit` (423+) calls `_replayed` before reading the file, so an edit that already landed is never re-applied, whatever `new` contains. | `test_append_edit_is_not_applied_twice_after_a_crash`, `test_receipt_without_the_write_redoes_the_write` (simulated `OSError` on the file replace after the receipt) |
| 10.2 | API/terminal command refusals not audited (item 8) | `_exec_permission` (`app/api/workspace.py:81`) wraps `ws.plan(argv)` and `_require(...)` in `try`; on `AppError` it calls the new `Workspace.record_rejection(actor, "exec", shlex.join(argv), exc)` (`jail.py:244-250`; status `rejected` if the error is a 403, else `error`) and re-raises. Covers `command_not_allowed`, jail errors on command arguments, and `permission_denied`, for both `POST /exec` and the terminal. | `test_audit_log_records_rejections_and_is_tenant_scoped` now expects `("exec","rejected")` with `command_not_allowed` for `curl x` — **integration, not re-run** |
| 10.3 | Audit failure turned a completed write into a tool error (item 4, first half) | `_record` (212-242) catches any exception from the sink and logs it with `logger.exception("workspace audit write failed", extra=<entry>)`. The audit is now explicitly best effort. | `test_audit_failure_does_not_fail_a_completed_write` (sink raises; write returns, file written) |
| 10.4 | Duplicate guard refused identical reads (item 9); no `call_id` on the event (item 13) | `select_or_execute_tools` (`nodes.py:217`, guard at ~259-275) refuses a repeated signature only when the tool has **`idempotent = False`** (`and not tool.idempotent`, 265). Reads/list/search keep the base default `idempotent = True` and run again. The `duplicate_call` ledger event now carries `data={"call_id": call.id}`. | `test_rereading_a_file_is_allowed` (two identical `workspace_read` → both `ok`); `test_identical_write_after_success_is_refused_without_approval` still passes |
| 10.5 | `grep -R` follows symlinks with GNU grep (item 10) | `-R` removed from the grep rule's flags (`executor.py:80-85`, comment "no -R: GNU grep follows symlinks"); `-r` stays. | none specific — `-R` is not in `test_commands_outside_the_allowlist_are_rejected`'s cases |
| 10.6 | Non-JSON terminal frame killed the session (item 11, first half) | `receive_json()` is now in its own `try`; `ValueError` → `{"type":"error","code":"invalid_request","message":"frames must be JSON"}` and `continue` (`app/api/workspace.py:186-192`). | `test_terminal_streams_output_and_enforces_policy` sends `"not json"`, expects `invalid_request`, then runs `ls` to show the session survives — **integration, not re-run** |
| 10.7 | Permissions frozen in the checkpoint (item 6, second half) | `AgentService.decide(..., permissions=None)` (`service.py:127-151`): if given, `ctx.permissions = ctx.permissions & permissions` after `restore_context` — tighten, never widen. The approve/reject routes pass `principal.permissions`; the CLI `_decide` passes `services.auth.permissions_for(tenant)`. A revoked grant makes the approved call fail the harness permission check (`tool_not_permitted`), so nothing is written. | none specific found |

Recorder notes on the fixes (open, not fixed):
- **The duplicate guard still covers all of `workspace_exec`**, because the tool is
  `idempotent = False` as a whole. So a *read-only* command repeated with identical argv (`cat
  calc/mathlib.py` to check an edit) is refused, and so is the natural "run tests → fix → run the
  same `python -m pytest -q` again" loop: the second identical pytest call gets `duplicate_call`.
  Guarding on `plan(argv).writes` for exec, or exempting test runs, would fix it.
- `test_append_edit_is_not_applied_twice_after_a_crash` replays the same call id *after* a
  completed edit (receipt and file both present); it would also have passed before the fix. The
  write-ahead behaviour itself is exercised by `test_receipt_without_the_write_redoes_the_write`.
- `_replayed` decides by content hash: if someone changes the file between a crash and the replay,
  the call is redone on the new content (for `edit`, only if `old` still occurs exactly once).
  `_replayed` also reads the whole file without the size cap or `O_NOFOLLOW` used elsewhere.
- With 10.3 a write can now succeed with **no audit row** (only a log line). That is the intended
  trade-off (the operation's truth is the file and the ledger), but the audit table is no longer
  complete by construction.
- The agent's own policy-stage rejections (`approval_needed` → `plan`) are still only in
  `execution_events`, not `workspace_audit`; 10.2 covers the user paths.
- No test asserts that `-R` is now rejected, and none covers the permission intersection in
  `decide`.

**Later refinement (M8, see journal 08 §1):** the duplicate guard now refuses only a repeat of the
LAST side-effecting call (`completed[-1:] == [signature]` with `_has_side_effect`; reads and
read-only commands are never refused, "test, edit, test" is allowed —
`test_rerunning_tests_after_an_edit_is_allowed`), which resolves the first Recorder note above; its
result is worded `{"status": "already_applied", ...}` instead of an error; and workspace reports
are never `insufficient_evidence` (`test_workspace_report_is_never_insufficient_evidence`).

Still open from §9 (unchanged): not an OS sandbox (1); TOCTOU (3); `.env.example` denied (5);
per-tenant all-or-nothing workspace permissions, no per-key scopes (6); loop mode read-only (7);
silent-command disconnect detection in the terminal (11); `.env.example`/README workspace entries
(12 — the build session will add them); `workspace_root` inside a repository not rejected (12);
receipts never cleaned up (13); a `workspace_task` with no tool results says "No relevant passages
were found in your documents." (13).

---

## 11. What I'd do differently at 100x scale

- **Real isolation per execution:** each command (or each run) in a short-lived container or
  microVM (gVisor/Firecracker) with the tenant volume mounted, no network, CPU/memory/pids limits,
  non-root UID. The path jail stays as defence in depth and for good error messages.
- **File operations by handle, not path:** `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)` on
  Linux closes the TOCTOU window completely.
- **Transactional idempotency:** write the intent row (`execution_id, call_id, pre-hash`) to
  Postgres before the write and the result after; on replay compare the file's current hash with
  the recorded post-hash instead of trusting "old text still present".
- **Audit through an outbox:** record the attempt *before* acting, update with the outcome, and
  never fail the user operation because the audit sink is slow — ship rows asynchronously.
- **Scopes per API key** (read / write / exec separately), with the approval policy per tenant
  (e.g. some tenants auto-approve `mkdir`).
- **Workspaces on object storage or per-tenant volumes**, not one shared host directory; quotas.
- **Terminal as a job:** `POST /exec` returns a job id; output streams from a log with offsets so a
  reconnecting UI can resume; explicit cancel.
