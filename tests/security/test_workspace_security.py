"""Workspace sandbox (product.txt 9.1): the path jail, the command allowlist, the scrubbed
environment, and the resource limits. Real files, real subprocesses, no mocks."""

import subprocess
import uuid
from pathlib import Path

import pytest

from app.core.errors import (
    AppError,
    CommandNotAllowedError,
    FileTooLargeError,
    InvalidRequestError,
    NotFoundError,
    UnsupportedFileTypeError,
    WorkspaceAccessError,
)
from app.workspace.audit import MemoryAudit
from app.workspace.executor import scrubbed_env
from app.workspace.jail import Actor, Workspace, WorkspaceManager

USER = Actor("user", key_id="test")


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "passwd").write_text("root:x:0:0\n")
    return secret


@pytest.fixture
def audit() -> MemoryAudit:
    return MemoryAudit()


@pytest.fixture
def ws(tmp_path: Path, audit: MemoryAudit) -> Workspace:
    manager = WorkspaceManager(
        tmp_path / "root", audit, max_read_bytes=2000, max_write_bytes=5000, exec_timeout_s=20
    )
    workspace = manager.for_tenant("tenant_a")
    (workspace.root / "notes.md").write_text("# Notes\nRPO is 5 minutes.\n")
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    return workspace


# --- path jail ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../outside/passwd", "src/../../outside/passwd", "/etc/passwd", "~/.ssh/id_rsa", "a\x00b"],
)
async def test_path_traversal_and_absolute_paths_are_rejected(ws: Workspace, path: str) -> None:
    with pytest.raises(WorkspaceAccessError):
        await ws.read(path, USER)


async def test_symlink_escape_is_rejected(ws: Workspace, outside: Path) -> None:
    (ws.root / "innocent.txt").symlink_to(outside / "passwd")
    (ws.root / "linkdir").symlink_to(outside, target_is_directory=True)
    for path in ("innocent.txt", "linkdir/passwd", "linkdir"):
        with pytest.raises(WorkspaceAccessError, match="outside"):
            await ws.read(path, USER)
    with pytest.raises(WorkspaceAccessError):
        await ws.write("linkdir/new.txt", "x", USER)
    assert not (outside / "new.txt").exists()
    listing = await ws.list_dir(".", USER)  # listed as links, never followed
    assert {e.name: e.type for e in listing.entries}["innocent.txt"] == "symlink"


async def test_symlink_to_a_secret_inside_the_root_is_rejected(ws: Workspace) -> None:
    (ws.root / ".env").write_text("AGENTOPS_API_KEYS=secret\n")
    (ws.root / "config.txt").symlink_to(ws.root / ".env")
    with pytest.raises(WorkspaceAccessError, match="sensitive"):
        await ws.read("config.txt", USER)


@pytest.mark.parametrize(
    "path", [".env", ".env.local", "keys/server.pem", ".ssh/config", "id_rsa", ".git/config"]
)
async def test_sensitive_names_are_denied_even_inside_the_root(ws: Workspace, path: str) -> None:
    target = ws.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret")
    with pytest.raises(WorkspaceAccessError, match="sensitive"):
        await ws.read(path, USER)
    with pytest.raises(WorkspaceAccessError):
        await ws.write(path, "overwritten", USER)
    assert target.read_text() == "secret"
    assert target.name not in {e.name for e in (await ws.list_dir(".", USER)).entries}


async def test_git_internals_are_read_only(ws: Workspace) -> None:
    with pytest.raises(WorkspaceAccessError, match="git internals"):
        await ws.write(".git/hooks/pre-commit", "#!/bin/sh\nrm -rf /\n", USER)


async def test_tenants_cannot_reach_each_other(ws: Workspace) -> None:
    other = WorkspaceManager(ws.root.parent, MemoryAudit()).for_tenant("tenant_b")
    with pytest.raises(WorkspaceAccessError):
        await other.read("../tenant_a/notes.md", USER)
    with pytest.raises(WorkspaceAccessError):
        WorkspaceManager(ws.root.parent, MemoryAudit()).for_tenant("../tenant_a")


# --- file operations ------------------------------------------------------------------------


async def test_read_limits_binary_and_missing_files(ws: Workspace) -> None:
    (ws.root / "big.txt").write_text("é" * 3000)  # 2 bytes each: the cut lands mid-character
    big = await ws.read("big.txt", USER)
    assert big.truncated and big.size == 6000 and len(big.content) == 1000
    (ws.root / "image.png").write_bytes(b"\x89PNG\r\n\x00\x00binary")
    with pytest.raises(UnsupportedFileTypeError):
        await ws.read("image.png", USER)
    with pytest.raises(NotFoundError):
        await ws.read("missing.md", USER)


async def test_write_is_size_capped_text_only_and_atomic(ws: Workspace) -> None:
    result = await ws.write("docs/new/plan.md", "# Plan\n", USER)
    assert result.created and result.path == "docs/new/plan.md" and result.bytes == 7
    assert (ws.root / "docs/new/plan.md").read_text() == "# Plan\n"
    with pytest.raises(FileTooLargeError):
        await ws.write("huge.md", "x" * 5001, USER)
    with pytest.raises(UnsupportedFileTypeError):
        await ws.write("bin.dat", "a\x00b", USER)
    assert not list(ws.root.rglob(".agentops-*.tmp"))  # no temp files left behind


async def test_edit_replaces_exactly_one_occurrence(ws: Workspace) -> None:
    await ws.edit("notes.md", "5 minutes", "300 seconds", USER)
    assert "RPO is 300 seconds." in (ws.root / "notes.md").read_text()
    with pytest.raises(InvalidRequestError, match="not found"):
        await ws.edit("notes.md", "5 minutes", "x", USER)
    (ws.root / "dup.md").write_text("a a")
    with pytest.raises(InvalidRequestError, match="2 times"):
        await ws.edit("dup.md", "a", "b", USER)


async def test_agent_writes_are_idempotent_per_tool_call(ws: Workspace) -> None:
    """A replayed approved call (same execution + call id) must not write twice."""
    agent = Actor("agent", execution_id=uuid.uuid4(), call_id="call_1")
    first = await ws.edit("notes.md", "5 minutes", "6 minutes", agent)
    replay = await ws.edit("notes.md", "5 minutes", "6 minutes", agent)
    assert replay.replayed and replay.sha256 == first.sha256
    assert (ws.root / "notes.md").read_text().count("6 minutes") == 1


async def test_search_is_literal_and_skips_secrets(ws: Workspace) -> None:
    (ws.root / ".env").write_text("RPO=leak\n")
    result = await ws.search("rpo", USER)
    assert [(m.path, m.line) for m in result.matches] == [("notes.md", 2)]
    assert (await ws.search("(a+)+$", USER)).matches == []  # literal: no regex, no ReDoS
    only_py = await ws.search("return", USER, glob="**/*.py")
    assert [m.path for m in only_py.matches] == ["src/app.py"]


# --- commands -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "."],
        ["bash", "-c", "id"],
        ["sh", "-c", "cat /etc/passwd"],
        ["python", "-c", "print(1)"],
        ["curl", "http://example.com"],
        ["git", "push"],
        ["git", "-C", "/", "status"],
        ["git", "diff", "--output=../outside/x"],
        ["find", ".", "-exec", "rm", "{}", ";"],
        ["find", ".", "-delete"],
        ["grep", "-f", "/etc/passwd", "x"],
        ["ls", "--color=always"],
        ["/bin/ls"],
    ],
)
async def test_commands_outside_the_allowlist_are_rejected(
    ws: Workspace, audit: MemoryAudit, argv: list[str]
) -> None:
    with pytest.raises(CommandNotAllowedError):
        await ws.exec(argv, USER)
    assert audit.entries[-1].status == "rejected"


@pytest.mark.parametrize(
    "argv",
    [["cat", "/etc/passwd"], ["cat", "../outside/passwd"], ["ls", "/"], ["grep", "-r", "x", "/"]],
)
async def test_command_path_arguments_go_through_the_jail(ws: Workspace, argv: list[str]) -> None:
    with pytest.raises(WorkspaceAccessError):
        await ws.exec(argv, USER)


async def test_shell_metacharacters_are_plain_arguments(ws: Workspace, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    result = await ws.exec(["ls", f"$(touch {marker})"], USER)
    assert result.exit_code != 0 and not marker.exists()


async def test_read_only_commands_run_in_the_workspace(ws: Workspace) -> None:
    ls = await ws.exec(["ls", "-la"], USER)
    assert ls.exit_code == 0 and "notes.md" in ls.output
    grep = await ws.exec(["grep", "-rn", "RPO", "."], USER)
    assert "notes.md:2:RPO is 5 minutes." in grep.output
    assert ws.plan(["git", "status"]).writes is False
    assert ws.plan(["python", "-m", "pytest", "-q"]).writes is True
    assert ws.plan(["pytest", "--collect-only"]).writes is True  # collection runs code


async def test_git_never_escapes_to_an_enclosing_repository(tmp_path: Path) -> None:
    outer = tmp_path / "repo"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)  # noqa: ASYNC221
    workspace = WorkspaceManager(outer / "ws", MemoryAudit()).for_tenant("tenant_a")
    result = await workspace.exec(["git", "status"], USER)
    assert result.exit_code != 0 and "not a git repository" in result.output.lower()


async def test_secrets_are_not_in_the_command_environment(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOPS_API_KEYS", "super-secret-key-123:tenant_a")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    (ws.root / "test_env.py").write_text(
        "import os\n\ndef test_env():\n    print(sorted(os.environ))\n"
        "    assert 'AGENTOPS_API_KEYS' not in os.environ\n"
        "    values = ' '.join(os.environ.values())\n"
        "    assert 'super-secret' not in values and 'aws-secret' not in values\n"
    )
    result = await ws.exec(["python", "-m", "pytest", "-q", "-s", "test_env.py"], USER)
    assert result.exit_code == 0, result.output
    assert "super-secret" not in result.output and "1 passed" in result.output
    env = scrubbed_env(Path("/h"), Path("/c"))
    assert not any(k.startswith(("AGENTOPS", "AWS", "OPENAI")) for k in env)


async def test_timeout_kills_the_process(tmp_path: Path) -> None:
    ws = WorkspaceManager(tmp_path / "root", MemoryAudit(), exec_timeout_s=1).for_tenant("t")
    (ws.root / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n")
    result = await ws.exec(["python", "-m", "pytest", "-q", "test_slow.py"], USER)
    assert result.timed_out and result.exit_code is None and result.duration_ms < 10_000


async def test_output_is_capped(tmp_path: Path) -> None:
    ws = WorkspaceManager(tmp_path / "root", MemoryAudit(), max_output_bytes=1000).for_tenant("t")
    (ws.root / "big.txt").write_text("line\n" * 10_000)
    result = await ws.exec(["cat", "big.txt"], USER)
    assert result.truncated and len(result.output) == 1000 and result.exit_code is None


async def test_every_operation_is_audited_including_rejections(
    ws: Workspace, audit: MemoryAudit
) -> None:
    await ws.read("notes.md", USER)
    with pytest.raises(WorkspaceAccessError):
        await ws.read("../outside/passwd", USER)
    await ws.exec(["ls"], USER)
    assert [(e.operation, e.status) for e in audit.entries] == [
        ("read", "ok"),
        ("read", "rejected"),
        ("exec", "ok"),
    ]
    assert all(e.tenant_id == "tenant_a" and e.actor == "user" for e in audit.entries)
    assert "RPO" not in str([e.model_dump() for e in audit.entries])  # no file content


async def test_append_edit_is_not_applied_twice_after_a_crash(ws: Workspace) -> None:
    """Recorder finding: an append-style edit (new contains old) replayed after a crash between
    the file replace and the receipt would have been applied twice. Receipts are write-ahead now."""
    agent = Actor("agent", execution_id=uuid.uuid4(), call_id="append")
    old, new = "RPO is 5 minutes.", "RPO is 5 minutes.\nRTO is 15 minutes."
    await ws.edit("notes.md", old, new, agent)
    replay = await ws.edit("notes.md", old, new, agent)  # same call id: no second append
    assert replay.replayed and (ws.root / "notes.md").read_text().count("RTO") == 1


async def test_receipt_without_the_write_redoes_the_write(ws: Workspace) -> None:
    """Crash after the write-ahead receipt but before the replace: the replay must write."""
    agent = Actor("agent", execution_id=uuid.uuid4(), call_id="w")
    ws._replace = lambda target, data: (  # type: ignore[method-assign]
        Workspace._replace(target, data) if target.suffix == ".json" else _crash()
    )
    with pytest.raises(AppError, match="I/O"):
        await ws.write("plan.md", "v1", agent)
    assert not (ws.root / "plan.md").exists()
    del ws._replace
    result = await ws.write("plan.md", "v1", agent)
    assert not result.replayed and (ws.root / "plan.md").read_text() == "v1"


def _crash() -> None:
    raise OSError("simulated crash before replace")


async def test_audit_failure_does_not_fail_a_completed_write(tmp_path: Path) -> None:
    class Broken(MemoryAudit):
        async def record(self, entry: object) -> None:
            raise RuntimeError("audit db down")

    ws = WorkspaceManager(tmp_path / "root", Broken()).for_tenant("t")
    result = await ws.write("a.md", "x", USER)
    assert result.bytes == 1 and (ws.root / "a.md").read_text() == "x"
