# Interview Prep 06 — Sandboxed Workspace Tools, Workspace API, Terminal (Day 3, M6)

Every answer is grounded in the uncommitted M6 working tree on branch
`day-3-workspace-observability-eval` (on top of `b497479`). Format: direct answer, then the
reasoning, then a code pointer. Line numbers are from the tree at the time of writing; function
names are the stable reference. Companion: `docs/journal/06-workspace-tools.md`. Answers reflect the code **after** the
fixes made following the Recorder review (journal 06 §10); where behaviour changed, the answer says
what it was before.

---

## Basic

### Q1. What can the agent do in the workspace, and what needs a human?
**Answer:** Six tools on the tenant's directory: `workspace_list`, `workspace_read`,
`workspace_search` (read-only, run directly), `workspace_write` and `workspace_edit` (always need
approval), and `workspace_exec` (needs approval only if the command is a write/exec command:
`python -m pytest`, `pytest`, `git add`, `mkdir`; read-only commands like `ls`, `grep`, `git
status` run directly).
Pointer: `app/tools/workspace.py` (classes `WorkspaceList` … `WorkspaceExec`, `workspace_tools`
210-218); `app/workspace/executor.py:RULES` (72-136).

### Q2. Where does a tenant's workspace live and how is the tenant chosen?
**Answer:** `<workspace_root>/<tenant_id>/`. The tenant comes from the `ExecutionContext`
(`ctx.tenant_id`), never from the model's arguments, and must match `^[A-Za-z0-9_-]{1,64}$`, so
`../tenant_a` cannot be a tenant.
Pointer: `app/tools/workspace.py:_WorkspaceTool._ws` (38-39); `app/workspace/jail.py:Workspace.__init__`
(153-164), `_TENANT_ID` (50).

### Q3. What does `Workspace.resolve` check, in order?
**Answer:** NUL / over-long path → reject; absolute or `~` → reject; any `..` component →
reject; `os.path.realpath(root / path)`; the result must be the root or start with `root +
os.sep`; then the deny-list on both the requested and the resolved relative path.
Pointer: `app/workspace/jail.py:Workspace.resolve` (168-183).

### Q4. Which files are off limits even inside the root?
**Answer:** Any path component named `.ssh .env .netrc .git-credentials .pypirc .npmrc .aws
.gnupg`, starting with `.env.`, `id_rsa`, `id_ecdsa`, `id_ed25519`, or ending in `.pem .key .p12
.pfx`; `.git/config` for reads; anything under `.git` for writes. Denied entries are also hidden
from directory listings and skipped by search.
Pointer: `app/workspace/jail.py:denied_reason` (60-76), `list_dir` (262-263), `search` (321-334).

### Q5. How does the executor run a command?
**Answer:** `asyncio.create_subprocess_exec(*exec_argv)` — never a shell — with `cwd` = tenant
root, a scrubbed environment, `stdin=DEVNULL`, stderr merged into stdout, and a new session so
the whole process group can be killed. Output streams as decoded text, then one `ExecResult`.
Pointer: `app/workspace/executor.py:run_streaming` (258-316).

### Q6. What is in the command environment?
**Answer:** Only what `scrubbed_env` builds: a fixed `PATH`, a per-tenant `HOME`, locale/terminal
vars, git hardening (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_TERMINAL_PROMPT=0`, `GIT_CEILING_DIRECTORIES`, …) and two Python flags. Nothing is copied
from the server, so API keys and DB URLs never reach a command. A test runs a real pytest
subprocess that asserts `AGENTOPS_API_KEYS` and `AWS_SECRET_ACCESS_KEY` are absent.
Pointer: `app/workspace/executor.py:scrubbed_env` (232-250);
`tests/security/test_workspace_security.py::test_secrets_are_not_in_the_command_environment`.

### Q7. What workspace endpoints exist?
**Answer:** Under `/api/v1/workspace`: `GET /tree`, `GET /file`, `PUT /file`, `GET /search`,
`POST /exec` (`argv` or `line`), `GET /audit`, and `WS /terminal`. All tenant-scoped through the
API key and permission-checked.
Pointer: `app/api/workspace.py`.

### Q8. What is recorded in the workspace audit log?
**Answer:** One row per operation handled by the jail/executor: tenant, actor (`agent`/`user`),
execution id or key fingerprint, operation, target (path or command line, never contents),
status `ok/rejected/error`, error code, exit code, bytes, duration. Stored in `workspace_audit`;
`GET /workspace/audit` lists it newest first. Since the post-review fixes, user commands refused
at planning/permission time are recorded too (`Workspace.record_rejection`), and the audit is
**best effort**: a failing insert is logged, not raised.
Pointer: `app/workspace/audit.py:AuditEntry`, `DbAudit`; `app/workspace/jail.py:_record` (212-242),
`record_rejection` (244-250); `app/db/models.py:WorkspaceAuditEvent`; migration
`0003_workspace_audit.py`.

### Q9. How does a tenant get workspace permissions?
**Answer:** From server configuration: `ApiKeyAuthenticator.permissions_for(tenant)` grants
`workspace:read/write/exec` to tenants listed in `AGENTOPS_WORKSPACE_TENANTS` (empty in `dev` means
`tenant_a`); everyone else gets `documents:read` only. The principal carries them; the API and CLI
pass them into `new_context`.
Pointer: `app/core/security.py` (20-21, `permissions_for` 57-61); `app/core/config.py:workspace_tenant_ids`.

### Q10. How do you approve a paused write from the terminal?
**Answer:** `agentops approvals` lists pending decisions with tool and arguments; `agentops
approve <id>` (or `reject <id> --reason …`) calls `AgentService.decide`, which claims the approval
with the conditional UPDATE, restores the context from the Postgres checkpoint and resumes the
graph — in a fresh process.
Pointer: `app/cli.py:approvals` (174), `_decide` (187), `approve` (198), `reject` (204).

---

## Logical

### Q11. Why call `realpath` before the containment check rather than after?
**Answer:** Because the check must apply to where the path *really* points. `innocent.txt →
/outside/passwd` is lexically inside the root; only after resolving symlinks does it show up as
outside. The test creates a file link, a directory link, and writes through the directory link.
Pointer: `app/workspace/jail.py:resolve` (176-178);
`tests/security/test_workspace_security.py::test_symlink_escape_is_rejected`.

### Q12. Why apply the deny-list to both the requested and the resolved path?
**Answer:** Requested-only misses `config.txt → .env` (a harmless name pointing at a secret);
resolved-only misses a request like `.env` that is itself a symlink to an allowed file. Checking
both closes both.
Pointer: `app/workspace/jail.py:resolve` (180);
`test_symlink_to_a_secret_inside_the_root_is_rejected`.

### Q13. Why is the containment check `startswith(root + os.sep)` and not `startswith(root)`?
**Answer:** `/ws/tenant_a_evil/x` starts with `/ws/tenant_a`. Adding the separator makes it a
directory-boundary check; equality handles the root itself.
Pointer: `app/workspace/jail.py:resolve` (177).

### Q14. Why validate options per command instead of just allowlisting program names?
**Answer:** Options are where "read-only" programs hide writes and escapes: `find -delete`,
`find -exec`, `grep -f /etc/passwd`, `git diff --output=FILE`, `git -C /`. Each `CommandRule`
lists allowed flags and value flags; anything else is `command_not_allowed`. Positionals are paths
and go through the jail.
Pointer: `app/workspace/executor.py:plan_command` (173-229);
`test_commands_outside_the_allowlist_are_rejected` (13 cases).

### Q15. Why is `pytest --collect-only` a write/exec command when product.txt lists it as read-only?
**Answer:** Collection imports test modules and `conftest.py`, i.e. runs arbitrary workspace code.
Classifying it read-only would let the agent execute code without a human. Deliberate deviation,
asserted by a test.
Pointer: `app/workspace/executor.py` (comment 116-117, `_PYTEST_FLAGS` 69);
`test_read_only_commands_run_in_the_workspace` (`ws.plan(["pytest","--collect-only"]).writes is True`).

### Q16. Why is `workspace_search` literal instead of regex?
**Answer:** A model- or user-supplied regex like `(a+)+$` can backtrack catastrophically, and the
search runs in `asyncio.to_thread`, which the tool timeout cannot interrupt — the thread would keep
burning CPU. Substring matching is linear. The test searches `(a+)+$` literally and gets no match.
Pointer: `app/workspace/jail.py:search` docstring (309-310);
`test_search_is_literal_and_skips_secrets`.

### Q17. How does a disallowed agent command get rejected *without* creating an approval?
**Answer:** `WorkspaceExec.approval_needed` calls `plan(argv)`, which raises
`CommandNotAllowedError` / `WorkspaceAccessError`. The harness calls `approval_needed` inside a
`try` and turns an `AppError` into a normal rejection (`tool_call/rejected`) before the approval
branch. The test asserts no `approval` event and `error_code == "command_not_allowed"`.
Pointer: `app/tools/workspace.py:WorkspaceExec.approval_needed` (194-196);
`app/agents/harness.py:call_tool` (229-240);
`tests/unit/test_workspace_agent.py::test_disallowed_command_is_rejected_before_any_approval`.

### Q18. What does `requires_approval` mean now that `approval_needed` exists?
**Answer:** "May need approval." It is the class-level flag the custom loop uses to drop tools it
cannot pause for; `approval_needed(args, ctx)` is the per-call decision (default: return
`requires_approval`). `workspace_exec` sets `requires_approval = True` but returns
`plan(argv).writes`.
Pointer: `app/tools/base.py:Tool.approval_needed` (32-36); `app/agents/custom_loop.py:_run`
(filter on `requires_approval`).

### Q19. Why does a user's `POST /exec` not need approval, but the agent's does?
**Answer:** The user is the human; asking them to approve their own command adds nothing. What
separates read from write for users is the permission: read-only commands need `workspace:read`,
write commands `workspace:exec` (`_exec_permission` plans the command to decide). The executor,
allowlist, jail and audit are the same.
Pointer: `app/api/workspace.py:_exec_permission` (81-83), `exec_command` (129-136).

### Q20. Why does a workspace task skip retrieval and loop over tools?
**Answer:** `classify_request` sets `needs_retrieval=False, needs_tools=True` for
`workspace_task`, so `plan_workflow` returns `[]` with no model call and the graph goes to
`select_or_execute_tools`. Workspace tasks are multi-step (read → edit → check), so `after_tools`
loops back while the last pass executed something and `tool_rounds < MAX_TOOL_ROUNDS (4)`.
Pointer: `app/agents/nodes.py:classify_request` (165-179), `plan_workflow` (183-190),
`after_tools` (500-511), `MAX_TOOL_ROUNDS` (29).

---

## Advanced

### Q21. How do idempotency receipts prevent a double write on replay?
**Answer:** The harness sets `ctx.current_call_id` while a tool runs; write tools build
`Actor("agent", execution_id, call_id)`. `_receipt_path` hashes `execution_id:call_id` to a file in
`<root>/.receipts/<tenant>/` (outside the tenant root). Receipts are **write-ahead**:
`_write_atomic` computes the result (including the *intended* sha256), writes the receipt
atomically via `_replace`, and only then replaces the file. On a replay, `_replayed` returns the
stored result with `replayed=True` **only if the file's current sha256 equals the receipt's**;
otherwise the write never happened and is redone. `edit` checks `_replayed` before reading the
file. So a graph resumed from a checkpoint taken before the write was recorded does not write again.
(Before the review fix the receipt was written *after* the replace.)
Pointer: `app/agents/harness.py:call_tool` (252, 271); `app/workspace/jail.py:_receipt_path`
(360-364), `_replayed` (366-374), `_replace` (377-387), `_write_atomic` (389-406), `edit` (423+);
`test_agent_writes_are_idempotent_per_tool_call`, `test_receipt_without_the_write_redoes_the_write`.

### Q22. Was there a crash window, and is there still one?
**Answer:** There was. With the receipt written after `os.replace`, a crash between them meant the
replay ran again; for an append-style edit (`new = old + more`, the live run's shape) `old` still
occurred once, so the edit would be applied twice. Write-ahead closes it: crash *before* the file
replace → receipt sha ≠ file sha → redo once; crash *after* → shas match → stored result, no second
application. What remains: the decision is by content hash, so if the file is changed by someone
else between the crash and the replay, the call is redone on the new content (an `edit` only if
`old` still occurs exactly once). `_replayed` also reads the whole file without the size cap.
Pointer: `app/workspace/jail.py:_replayed` (366-374), `_write_atomic` (389-406);
`test_append_edit_is_not_applied_twice_after_a_crash` (note: it replays after a completed edit and
would also have passed before the fix), `test_receipt_without_the_write_redoes_the_write`;
journal 06 §10.1.

### Q23. Explain the graph's duplicate-call guard and why it exists.
**Answer:** `select_or_execute_tools` keeps `completed_calls`, the `call_signature` (name +
canonical JSON arguments) of every successful call. A newly *queued* call whose signature is already
there **and whose tool has `idempotent = False`** is refused before `call_tool`: ledger
`tool_call/rejected duplicate_call` with `data.call_id`, and a tool result telling the model it
already succeeded — no approval is created. Live, after an approved edit, qwen3:8b proposed the
identical edit again; approving it would have applied it twice because the anchor text survived.
Reads, list and search are idempotent and may repeat (the review fix — originally every tool was
guarded, so re-reading a file after an edit was refused). Still a gap: `workspace_exec` is
non-idempotent as a whole, so an identical read-only command or a repeated identical `python -m
pytest -q` is refused.
Pointer: `app/agents/nodes.py:select_or_execute_tools` (guard ~259-275, `and not tool.idempotent`
265); `app/agents/harness.py:call_signature` (46-48);
`test_identical_write_after_success_is_refused_without_approval`, `test_rereading_a_file_is_allowed`.

### Q24. How is the number of tool rounds bounded, and what counts as a round?
**Answer:** `tool_rounds` increments only when the model is asked for tools (not on resume passes
that execute approved calls). `round_executed` counts calls executed in the pass, including calls
that failed with a non-terminal error (so the model can correct a path). `after_tools` loops only
for `workspace_task`, when `round_executed > 0` and `tool_rounds < 4`. Duplicates and rejections
are not "executed", so they end the loop. The test feeds 10 distinct reads and sees exactly 4
`tools` model calls.
Pointer: `app/agents/nodes.py:select_or_execute_tools` (220-224, 294-312), `after_tools`;
`test_workspace_rounds_are_bounded`.

### Q25. Why build the answer schema per call with a citation enum?
**Answer:** Live, qwen3:8b put whole sentences into `citations` three drafts in a row, despite
the description and correction feedback, ending in `failed_validation`. `_draft_schema` replaces
`citations.items` with `{"type":"string","enum": <labels shown>}`; with Ollama's constrained
decoding the model cannot emit anything else. The re-run answered and verified as `supported`.
Pointer: `app/agents/nodes.py:_draft_schema` (151-158), `generate_answer` (376).

### Q26. How does the executor enforce timeouts and output caps, and why kill the process group?
**Answer:** `asyncio.timeout(timeout_s)` around the read loop; output beyond `max_output_bytes` sets
`truncated` and breaks. In `finally`, if the process is still alive, `os.killpg(pid, SIGKILL)` —
the process was started with `start_new_session=True`, so pytest's children die with it.
`exit_code=None` signals "killed". The same `finally` runs when a consumer closes the iterator
early. Tests: a 30 s sleep test is killed under 10 s with `timed_out`; `cat` of 50 KB is cut at
1000 bytes.
Pointer: `app/workspace/executor.py:_kill` (253-255), `run_streaming` (285-316);
`test_timeout_kills_the_process`, `test_output_is_capped`.

### Q27. How do you stop `git` from escaping the workspace or running code?
**Answer:** Four layers: `GIT_CEILING_DIRECTORIES=<workspace root>` so git never finds an
enclosing repository (tested with a workspace inside a real repo → "not a git repository");
`GIT_CONFIG_NOSYSTEM` + `GIT_CONFIG_GLOBAL=/dev/null`; `-c core.fsmonitor=false -c
core.hooksPath=/dev/null` injected on every git call (fsmonitor and hooks execute programs); and
`--no-ext-diff --no-textconv` on diff/log. `-C` is not an allowed option and `.git/**` is
read-only for the file tools.
Pointer: `app/workspace/executor.py` (`_GIT_HARDENING` 33, rules 93-114, `scrubbed_env`);
`test_git_never_escapes_to_an_enclosing_repository`, `test_git_internals_are_read_only`.

### Q28. What does the ledger store for a write, and what does the reviewer see?
**Answer:** The ledger (and so traces) stores `ledger_arguments`: path, sizes and a 16-hex sha256
of the content/`old`, plus a `result` summary (`bytes, sha256, created, replayed`). The reviewer
sees the **full** call because the approval payload comes from the `interrupt` value built from the
pending call in graph state. The test asserts `pending["arguments"]["new"] == "300 seconds"` and
`"old" not in edit.data["arguments"]`.
Pointer: `app/tools/workspace.py:WorkspaceWrite.ledger_arguments` (133-134),
`WorkspaceEdit.ledger_arguments` (161-167); `app/agents/nodes.py:human_approval` (321-323);
`test_edit_waits_for_approval_then_applies`.

---

## Scenario

### Q29. "The model wrote a file and the server crashed. On restart we resumed. Was it written twice?"
**Answer:** No. The resumed call has the same execution id and call id. Because the receipt is
written before the file, there are two cases: the file already has the receipt's sha256 → the
stored result comes back with `replayed=True` (visible in the ledger `result`), nothing is written;
or it does not → the crash came before the replace, and the write/edit runs exactly once now. Check
`workspace_audit` and the ledger for that execution. (Before the post-review fix, a crash between
the replace and a receipt written afterwards could re-apply an append-style edit.)
Pointer: `app/workspace/jail.py:_replayed`, `_write_atomic`;
`app/tools/workspace.py:WorkspaceWrite.ledger_output`.

### Q30. "A user says the terminal ran `grep -R password .` and printed lines from `/etc`." How, and is it still possible?
**Answer:** It was possible on a Linux host: the positional `.` is inside the jail, but GNU grep's
`-R` follows *all* symlinks during recursion, so a symlink already in the workspace pointing outside
(e.g. from a cloned repo) was traversed by grep itself — the jail only checks arguments. On this Mac
`/usr/bin/grep` is BSD grep, which (verified in a scratch directory) follows neither with `-r` nor
`-R`. Fixed after the review: `-R` was removed from the grep rule, so the command is now
`command_not_allowed`; `-r` stays (GNU `-r` follows only symlinks named on the command line, and
those go through the jail). No test asserts the `-R` rejection yet. `workspace_search` was never
affected (`os.walk(followlinks=False)`, links skipped).
Pointer: `app/workspace/executor.py:RULES` (grep rule 79-85, "no -R: GNU grep follows symlinks");
journal 06 §10.5.

### Q31. "Security asks for every rejected command in the audit log. Some were missing." Why, and what now?
**Answer:** `POST /exec` and the terminal call `_exec_permission` → `ws.plan(argv)` before
`ws.stream`, where command rejections used to be audited; a `command_not_allowed`, jail, or
`permission_denied` error raised there returned 403 with no row. Fixed: `_exec_permission` catches
the `AppError` and calls `Workspace.record_rejection(actor, "exec", shlex.join(argv), exc)` before
re-raising. The integration test now expects an `("exec", "rejected", command_not_allowed)` row
(not re-run yet — DB down). Still not in `workspace_audit`: the *agent's* policy-stage rejections
(`approval_needed` → `plan`), which are only in `execution_events`; and, since the audit became best
effort, operations whose audit insert failed (logged instead).
Pointer: `app/api/workspace.py:_exec_permission` (81-90); `app/workspace/jail.py:record_rejection`
(244-250), `_record` (237-242); journal 06 §10.2-§10.3.

### Q32. "After the agent edited a file, it said the file still had the old content." Why?
**Answer:** Before the post-review fix, the duplicate guard: the model re-issued `workspace_read`
with the same arguments to check its edit, the signature matched the earlier successful read, and
it got `duplicate_call`. Now only `idempotent = False` tools are guarded, so `workspace_read` runs
again (`test_rereading_a_file_is_allowed`). It can still happen if the model checks via
`workspace_exec` with the same argv it used before (e.g. `["cat", "calc/mathlib.py"]`), because
`workspace_exec` is guarded as a whole. Fix for that: guard exec only when `plan(argv).writes`.
Pointer: `app/agents/nodes.py:select_or_execute_tools` (guard ~259-275);
`app/tools/workspace.py:WorkspaceExec` (`idempotent = False`).

### Q33. "We removed tenant_c from AGENTOPS_WORKSPACE_TENANTS; can a pending write of theirs still be applied?"
**Answer:** Not any more. Permissions are copied into the `ExecutionContext` at run start and
saved in every checkpoint (`snapshot()["permissions"]`); before the fix, `restore` alone decided, so
the approved write went through. Now `AgentService.decide(..., permissions=...)` intersects the
restored `ctx.permissions` with the deciding caller's *current* permissions (API:
`principal.permissions`; CLI: `services.auth.permissions_for(tenant)`) — tighten, never widen.
The approved call then fails the harness permission check (`tool_not_permitted`) and nothing is
written. No test covers this path yet.
Pointer: `app/core/execution_context.py:snapshot` (102), `restore` (130);
`app/agents/service.py:decide` (127-151, intersection 146); `app/api/agent.py` approve/reject;
`app/cli.py:_decide`.

---

## Trade-off

### Q34. Path jail + allowlist vs a container sandbox?
**Answer:** The jail and allowlist are cheap, testable, portable (works on a 16 GB laptop with no
Docker), and give precise error messages the model can act on. But they are not an OS boundary:
an approved `python -m pytest` runs workspace code as the server with the server's interpreter and
can read anything the server can. A container/microVM (no network, separate UID, read-only rootfs,
resource limits) is the real boundary; the jail stays as defence in depth. Chosen here: jail +
human approval for code execution, container documented as the production answer.
Pointer: `app/workspace/executor.py` module docstring (9-10).

### Q35. Dedicated audit table vs only `execution_events`?
**Answer:** product.txt asked for `execution_events`. But user API/terminal operations have no
execution, and an audit that disappears when an execution is deleted is not an audit. The
dedicated table (no FK) covers both actors; agent calls are additionally in the ledger, so an
execution trace stays complete. Cost: two places to look, and they can disagree — the agent's
policy-stage rejections are only in the ledger, and a failed audit insert is now only a log line.
Pointer: `app/db/models.py:WorkspaceAuditEvent` docstring; `app/workspace/audit.py` docstring.

### Q36. Duplicate guard in the graph vs relying on `edit`'s "exactly one occurrence"?
**Answer:** "Exactly one" stops ambiguous edits, not repeated ones: an append-style edit leaves its
anchor in place, so the second application also sees exactly one occurrence. The graph guard stops
the repeat before a human is asked. Cost: it lives in graph state (lost with a crash — the
write-ahead receipts cover that case), and its granularity is the tool's `idempotent` flag: reads
may repeat, but every identical `workspace_exec` is refused, including harmless re-runs of tests.
Pointer: `app/workspace/jail.py:edit` (423+); `app/agents/nodes.py:select_or_execute_tools`.

### Q37. Tenant-level workspace permissions in config vs per-key scopes?
**Answer:** Config per tenant was the smallest change that made the approval flow reachable and
keeps "permissions never come from the request". Per-key scopes would allow a read-only key, a CI
key with exec, etc., but need key storage beyond the current env-var list. Cost today:
all-or-nothing (`WORKSPACE_PERMISSIONS` is granted as a set). Paused runs are no longer frozen at
their old grant: `decide` intersects with the caller's current permissions.
Pointer: `app/core/security.py:permissions_for` (57-61); `app/agents/service.py:decide`.

---

## Justification

### Q38. "Why not just give the agent a shell with a blocklist of dangerous commands?"
**Answer:** A blocklist is never complete (`find -exec`, `python -c`, `git -c core.pager=…`,
`$(…)`), and a shell adds pipes, redirection, globbing and substitution as escape routes. No shell
means those do not exist; an allowlist of commands *and options* means only reviewed behaviour can
run. product.txt §9 explicitly forbids unrestricted shell execution.
Pointer: `app/workspace/executor.py` module docstring (1-11);
`test_shell_metacharacters_are_plain_arguments`.

### Q39. "Why does the WebSocket accept the API key in the first frame? Headers are standard."
**Answer:** Browsers' `new WebSocket(url)` cannot set custom headers, and putting the key in the
query string leaks it into access logs and browser history. So the server accepts `X-API-Key` if
present (CLI clients) and otherwise waits up to 10 s for `{"api_key": …}`; anything else closes
with 4401.
Pointer: `app/api/workspace.py:terminal` (158-171);
`tests/integration/test_workspace_api.py::test_terminal_streams_output_and_enforces_policy`.

### Q40. "Why refuse `.env.example`? It's not a secret."
**Answer:** The rule is a name-based prefix (`.env.`) because `.env.local`, `.env.production`, etc.
are real secrets and a jail cannot read file contents to decide. Refusing one harmless template is
the accepted price of not having to enumerate every `.env.*` variant.
Pointer: `app/workspace/jail.py:_SECRET_PREFIXES` (54), `denied_reason`.

### Q41. "Why only read-only workspace tools in loop mode?"
**Answer:** The custom loop has no checkpoint/interrupt, so it cannot pause for a human; offering a
tool that will always stop at "approval required" is a dead end (a Day 2 finding). The loop drops
every tool with `requires_approval`, which includes `workspace_exec` even for read-only commands,
leaving list/read/search. Writes and commands are graph-only.
Pointer: `app/agents/custom_loop.py:_run`;
`tests/unit/test_workspace_agent.py::test_loop_offers_only_read_only_workspace_tools`.

---

## System design

### Q42. Walk through "add a function to calc/mathlib.py" end to end.
**Answer:** `agentops run …` → `new_context` with `permissions_for("tenant_a")` (all workspace
tools offered) → `classify_request` → `workspace_task` (no retrieval) → `select_or_execute_tools`
round 1: model calls `workspace_read` → executed, `round_executed=1` → loop → round 2: model calls
`workspace_edit` → `approval_needed` is true → ledger `approval/pending` (hashed args) →
`ApprovalRequiredError` → `human_approval` interrupts, checkpoint saved → `_execute` saves
`awaiting_approval` and creates the approval row → CLI prints the approval id. Later, another
process: `agentops approve <id>` → conditional UPDATE claim → `restore_context` (permissions,
budget, ledger from checkpoint) → `resume(call_id)` → call id added to `approved_call_ids` →
`call_tool` sets `current_call_id` → `Workspace.edit` (jail, exactly-one, atomic write, receipt,
audit) → loop back → model proposes the same edit → duplicate guard refuses → `round_executed=0`
→ `generate_answer` with the citation enum → validate → verify → `answered`. (Live, the repeat
edit happened before the guard existed and created a second approval; the refusal path is
verified by the unit test, not yet by a repeat live run.)
Pointer: `app/agents/nodes.py`; `app/agents/harness.py:call_tool`; `app/workspace/jail.py:edit`;
journal 06 §4.

### Q43. How would you run this for many tenants in production?
**Answer:** Per-execution containers or microVMs with the tenant's volume mounted, no network,
resource limits and a non-root UID; file operations via `openat2(RESOLVE_BENEATH |
RESOLVE_NO_SYMLINKS)` to remove TOCTOU; per-key scopes (read / write / exec) and per-tenant
approval policies; transactional intent records instead of file receipts; audit written before the
action and shipped through an outbox; workspaces on per-tenant volumes with quotas; the terminal as
a job with resumable output offsets and an explicit cancel.
Pointer: journal 06 §11.

### Q44. Where do the security guarantees live, and what is *not* guaranteed?
**Answer:** In code, not the prompt: the jail (`resolve`, `denied_reason`), the command policy
(`plan_command`, `RULES`), the scrubbed environment, the harness's permission and approval checks,
write-ahead receipts, the duplicate guard, and the audit. Not guaranteed: isolation of approved
code execution (not an OS sandbox), TOCTOU against a concurrent symlink swap of a directory
component, audit completeness (best effort; agent policy-stage rejections only in the ledger), and
exactly-once edits if the file is changed by someone else between a crash and the replay. The
GNU `grep -R` symlink issue and the old receipt crash window were fixed after the review.
Pointer: `app/workspace/`; `app/agents/harness.py`; journal 06 §9-§10.
