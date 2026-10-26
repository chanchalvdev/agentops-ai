"""Per-tenant path jail and file operations (product.txt 9.1).

Every path goes through `Workspace.resolve`: relative only, no "..", then realpath (symlinks are
followed BEFORE the containment check, so a link pointing outside is caught), then it must land
inside the tenant's root. A deny-list of secret-looking names applies even inside the root, to
the requested path and to where it resolves. File operations are size-capped, refuse binary
content, write atomically (temp file + rename), and are audited, including rejections.

Writes made by the agent carry an idempotency key (execution id + tool call id). A receipt with
the intended content hash is written BEFORE the file is replaced. Replaying the same approved call
(e.g. a graph resumed from a checkpoint taken before the write was recorded) finds the receipt; if
the file already has that hash the stored result is returned instead of writing again, otherwise
the replace never happened and the write is redone. No crash window applies an edit twice.
"""

import asyncio
import hashlib
import logging
import os
import re
import shlex
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

from app.core.errors import (
    AppError,
    FileTooLargeError,
    InvalidRequestError,
    NotFoundError,
    UnsupportedFileTypeError,
    WorkspaceAccessError,
)
from app.workspace.audit import AuditEntry, AuditSink, AuditStatus, Operation
from app.workspace.executor import (
    CommandPlan,
    ExecResult,
    plan_command,
    run_streaming,
    scrubbed_env,
)

logger = logging.getLogger(__name__)
_TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_NAMES = frozenset(
    {".ssh", ".env", ".netrc", ".git-credentials", ".pypirc", ".npmrc", ".aws", ".gnupg"}
)
_SECRET_PREFIXES = (".env.", "id_rsa", "id_ecdsa", "id_ed25519")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"})
_MAX_LIST_ENTRIES, _MAX_SEARCH_FILES, _MAX_SEARCH_FILE_BYTES = 500, 2000, 1_000_000


def denied_reason(rel: PurePosixPath, *, write: bool) -> str | None:
    """Why a path inside the root is still off limits, or None."""
    parts = rel.parts
    for i, part in enumerate(parts):
        name = part.lower()
        if (
            name in _SECRET_NAMES
            or name.startswith(_SECRET_PREFIXES)
            or name.endswith(_SECRET_SUFFIXES)
        ):
            return "sensitive file"
        if name == ".git":
            if write:
                return "git internals are read-only"  # hooks/config there would run code
            if parts[i + 1 : i + 2] == ("config",):
                return "sensitive file"
    return None


@dataclass(frozen=True)
class Actor:
    kind: Literal["agent", "user"]
    execution_id: uuid.UUID | None = None
    call_id: str | None = None  # agent tool call: the idempotency key for writes
    key_id: str | None = None  # user: API key fingerprint


class Entry(BaseModel):
    name: str
    type: Literal["file", "dir", "symlink", "other"]
    size: int | None


class ListResult(BaseModel):
    path: str
    entries: list[Entry]
    truncated: bool


class FileContent(BaseModel):
    path: str
    content: str
    size: int
    truncated: bool


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str


class SearchResult(BaseModel):
    query: str
    matches: list[SearchMatch]
    files_scanned: int
    truncated: bool


class WriteResult(BaseModel):
    # Stated explicitly for the model: live, a bare {path, bytes, sha256} after an edit was
    # read as "nothing happened" next to the pre-edit file content from an earlier read.
    outcome: Literal["applied", "already_applied"] = "applied"
    path: str
    bytes: int
    sha256: str
    created: bool
    replayed: bool = False  # True: this call already ran; the stored receipt was returned


def _decode(data: bytes, truncated: bool) -> str:
    if b"\x00" in data[:8192]:
        raise UnsupportedFileTypeError("Binary files are not supported")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated and exc.start >= len(data) - 3:  # cut inside a multi-byte character
            return data[: exc.start].decode("utf-8")
        raise UnsupportedFileTypeError("File is not valid UTF-8 text") from None


def _map_os_error(exc: OSError) -> AppError:
    if isinstance(exc, FileNotFoundError):
        return NotFoundError("No such file or directory")
    if isinstance(exc, IsADirectoryError):
        return InvalidRequestError("Path is a directory")
    if isinstance(exc, NotADirectoryError):
        return InvalidRequestError("Path is not a directory")
    if isinstance(exc, PermissionError):
        return WorkspaceAccessError("Permission denied")
    return AppError("Workspace I/O error", detail=repr(exc))


class Workspace:
    """One tenant's directory. Create through WorkspaceManager.for_tenant."""

    def __init__(self, manager: "WorkspaceManager", tenant_id: str) -> None:
        if not _TENANT_ID.match(tenant_id):
            raise WorkspaceAccessError("Invalid tenant for a workspace")
        self.tenant_id = tenant_id
        self._m = manager
        base = manager.base
        (base / tenant_id).mkdir(parents=True, exist_ok=True)
        self.root = Path(os.path.realpath(base / tenant_id))
        # Outside the tenant root (tenant ids cannot start with "."), so the agent cannot
        # read or forge its own receipts or the command HOME.
        self._receipts = base / ".receipts" / tenant_id
        self._home = base / ".home" / tenant_id

    # --- path jail --------------------------------------------------------------------------

    def resolve(self, path: str, write: bool = False) -> Path:
        if "\x00" in path or len(path) > 1000:
            raise WorkspaceAccessError("Invalid path")
        requested = PurePosixPath(path or ".")
        if requested.is_absolute() or path.startswith("~"):
            raise WorkspaceAccessError("Absolute paths are not allowed; use a workspace path")
        if ".." in requested.parts:
            raise WorkspaceAccessError("'..' is not allowed in workspace paths")
        full = os.path.realpath(self.root / requested)
        if full != str(self.root) and not full.startswith(str(self.root) + os.sep):
            raise WorkspaceAccessError("Path resolves outside the workspace")
        rel = PurePosixPath(os.path.relpath(full, self.root))
        reason = denied_reason(requested, write=write) or denied_reason(rel, write=write)
        if reason:
            raise WorkspaceAccessError(f"Access denied: {reason}")
        return Path(full)

    def display(self, full: Path) -> str:
        return PurePosixPath(os.path.relpath(full, self.root)).as_posix()

    # --- audit ------------------------------------------------------------------------------

    async def _audited[T](
        self,
        actor: Actor,
        operation: Operation,
        target: str,
        fn: Callable[[], T],
        size: Callable[[T], int | None] = lambda _: None,
    ) -> T:
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(fn)
        except OSError as exc:
            mapped = _map_os_error(exc)
            await self._record(actor, operation, target, "error", started, mapped.code)
            raise mapped from exc
        except AppError as exc:
            status: AuditStatus = "rejected" if isinstance(exc, WorkspaceAccessError) else "error"
            await self._record(actor, operation, target, status, started, exc.code)
            raise
        await self._record(actor, operation, target, "ok", started, size=size(result))
        return result

    async def _record(
        self,
        actor: Actor,
        operation: Operation,
        target: str,
        status: AuditStatus,
        started: float,
        error_code: str | None = None,
        *,
        size: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        entry = AuditEntry(
            tenant_id=self.tenant_id,
            actor=actor.kind,
            execution_id=actor.execution_id,
            key_id=actor.key_id,
            operation=operation,
            target=target[:2000],
            status=status,
            error_code=error_code,
            exit_code=exit_code,
            bytes=size,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        try:
            await self._m.audit.record(entry)
        except Exception:
            # Best effort, after the fact: a failed audit insert must not turn a write that DID
            # happen into an error the agent would retry under a new call id.
            logger.exception("workspace audit write failed", extra=entry.model_dump(mode="json"))

    async def record_rejection(
        self, actor: Actor, operation: Operation, target: str, exc: AppError
    ) -> None:
        """Audit a request refused before it reached the jail (e.g. by the API permission check
        or command planning), so the log really contains every rejection."""
        status: AuditStatus = "rejected" if exc.status_code == 403 else "error"
        await self._record(actor, operation, target, status, time.perf_counter(), exc.code)

    # --- file operations --------------------------------------------------------------------

    async def list_dir(self, path: str, actor: Actor) -> ListResult:
        def run() -> ListResult:
            full = self.resolve(path)
            entries: list[Entry] = []
            truncated = False
            with os.scandir(full) as it:
                for item in sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name)):
                    rel = PurePosixPath(os.path.relpath(item.path, self.root))
                    if denied_reason(rel, write=False):
                        continue  # do not even reveal that secrets exist
                    if len(entries) >= _MAX_LIST_ENTRIES:
                        truncated = True
                        break
                    kind: Literal["file", "dir", "symlink", "other"]
                    if item.is_symlink():
                        kind = "symlink"
                    elif item.is_dir(follow_symlinks=False):
                        kind = "dir"
                    elif item.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        kind = "other"
                    size = item.stat(follow_symlinks=False).st_size if kind == "file" else None
                    entries.append(Entry(name=item.name, type=kind, size=size))
            return ListResult(path=self.display(full), entries=entries, truncated=truncated)

        return await self._audited(actor, "list", path, run, lambda r: len(r.entries))

    async def read(self, path: str, actor: Actor, max_bytes: int | None = None) -> FileContent:
        limit = min(max_bytes or self._m.max_read_bytes, self._m.max_read_bytes)

        def run() -> FileContent:
            full = self.resolve(path)
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                data = f.read(limit + 1)
            truncated = len(data) > limit
            text = _decode(data[:limit], truncated)
            return FileContent(
                path=self.display(full), content=text, size=size, truncated=truncated
            )

        return await self._audited(actor, "read", path, run, lambda r: len(r.content))

    async def search(
        self,
        query: str,
        actor: Actor,
        *,
        glob: str = "**/*",
        path: str = ".",
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> SearchResult:
        """Literal (fixed-string) search, like `grep -rF`. No regex: a user- or model-supplied
        pattern could backtrack catastrophically, and a thread cannot be interrupted."""
        if not query or len(query) > 200:
            raise InvalidRequestError("query must be 1 to 200 characters")
        needle = query if case_sensitive else query.lower()

        def run() -> SearchResult:
            top = self.resolve(path)
            matches: list[SearchMatch] = []
            scanned, truncated = 0, False
            for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
                rel_dir = PurePosixPath(os.path.relpath(dirpath, self.root))
                dirnames[:] = sorted(
                    d
                    for d in dirnames
                    if d not in _SKIP_DIRS and not denied_reason(rel_dir / d, write=False)
                )
                for name in sorted(filenames):
                    rel = PurePosixPath(os.path.normpath(rel_dir / name))
                    file = os.path.join(dirpath, name)
                    if (
                        denied_reason(rel, write=False)
                        or os.path.islink(file)
                        or not rel.full_match(glob)
                    ):
                        continue
                    if scanned >= _MAX_SEARCH_FILES or len(matches) >= max_results:
                        truncated = True
                        break
                    scanned += 1
                    with open(file, "rb") as f:
                        data = f.read(_MAX_SEARCH_FILE_BYTES)
                    try:
                        text = _decode(data, truncated=True)
                    except UnsupportedFileTypeError:
                        continue
                    for n, line in enumerate(text.splitlines(), 1):
                        if needle in (line if case_sensitive else line.lower()):
                            matches.append(
                                SearchMatch(path=rel.as_posix(), line=n, text=line[:300])
                            )
                            if len(matches) >= max_results:
                                break
                if truncated:
                    break
            return SearchResult(
                query=query, matches=matches, files_scanned=scanned, truncated=truncated
            )

        return await self._audited(actor, "search", f"{query!r} in {path}", run)

    def _receipt_path(self, actor: Actor) -> Path | None:
        if actor.kind != "agent" or not (actor.execution_id and actor.call_id):
            return None
        key = hashlib.sha256(f"{actor.execution_id}:{actor.call_id}".encode()).hexdigest()
        return self._receipts / f"{key}.json"

    def _replayed(self, full: Path, actor: Actor) -> WriteResult | None:
        """The stored result if this exact call already changed the file, else None."""
        receipt = self._receipt_path(actor)
        if receipt is None or not receipt.exists() or not full.is_file():
            return None
        stored = WriteResult.model_validate_json(receipt.read_text())
        if hashlib.sha256(full.read_bytes()).hexdigest() != stored.sha256:
            return None  # receipt written, replace never happened: do the write now
        return stored.model_copy(update={"replayed": True, "outcome": "already_applied"})

    @staticmethod
    def _replace(target: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".agentops-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _write_atomic(self, full: Path, data: bytes, actor: Actor) -> WriteResult:
        if (done := self._replayed(full, actor)) is not None:
            return done
        if full.is_dir():
            raise InvalidRequestError("Path is a directory")
        result = WriteResult(
            path=self.display(full),
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created=not full.exists(),
        )
        full.parent.mkdir(parents=True, exist_ok=True)
        receipt = self._receipt_path(actor)
        if receipt is not None:  # write-ahead: the receipt exists before the file changes
            receipt.parent.mkdir(parents=True, exist_ok=True)
            self._replace(receipt, result.model_dump_json().encode())
        self._replace(full, data)
        return result

    def _check_text(self, content: str) -> bytes:
        data = content.encode("utf-8")
        if len(data) > self._m.max_write_bytes:
            raise FileTooLargeError(f"Content exceeds {self._m.max_write_bytes} bytes")
        if "\x00" in content:
            raise UnsupportedFileTypeError("Binary content is not supported")
        return data

    async def write(self, path: str, content: str, actor: Actor) -> WriteResult:
        def run() -> WriteResult:
            full = self.resolve(path, write=True)
            return self._write_atomic(full, self._check_text(content), actor)

        return await self._audited(actor, "write", path, run, lambda r: r.bytes)

    async def edit(self, path: str, old: str, new: str, actor: Actor) -> WriteResult:
        """Replace exactly one occurrence of `old`. Zero or several matches are errors, so an
        edit never lands somewhere the caller did not mean."""

        def run() -> WriteResult:
            full = self.resolve(path, write=True)
            if (done := self._replayed(full, actor)) is not None:
                return done  # this call's edit is already in the file
            if not old:
                raise InvalidRequestError("old text must not be empty")
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                data = f.read(self._m.max_write_bytes + 1)
            if len(data) > self._m.max_write_bytes:
                raise FileTooLargeError("File is too large to edit")
            text = _decode(data, truncated=False)
            count = text.count(old)
            if count != 1:
                problem = "not found" if count == 0 else f"found {count} times; add context"
                raise InvalidRequestError(f"old text {problem}")
            return self._write_atomic(full, self._check_text(text.replace(old, new, 1)), actor)

        return await self._audited(actor, "edit", path, run, lambda r: r.bytes)

    # --- commands ---------------------------------------------------------------------------

    def plan(self, argv: list[str]) -> CommandPlan:
        """Validate a command without running it (used to decide whether approval is needed)."""
        return plan_command(argv, self.resolve)

    async def stream(self, argv: list[str], actor: Actor) -> AsyncIterator[str | ExecResult]:
        target = shlex.join(argv)[:2000]
        started = time.perf_counter()
        try:
            plan = self.plan(argv)
        except AppError as exc:
            status: AuditStatus = "rejected" if isinstance(exc, WorkspaceAccessError) else "error"
            await self._record(actor, "exec", target, status, started, exc.code)
            raise
        self._home.mkdir(parents=True, exist_ok=True)
        result: ExecResult | None = None
        try:
            async for item in run_streaming(
                plan,
                cwd=self.root,
                env=scrubbed_env(self._home, self.root.parent),
                timeout_s=self._m.exec_timeout_s,
                max_output_bytes=self._m.max_output_bytes,
            ):
                if isinstance(item, ExecResult):
                    result = item
                yield item
        finally:
            if result is None:  # failed to start, or the consumer stopped early
                await self._record(actor, "exec", target, "error", started, "exec_interrupted")
            else:
                await self._record(
                    actor,
                    "exec",
                    target,
                    "ok" if result.exit_code == 0 else "error",
                    started,
                    "timeout" if result.timed_out else None,
                    size=len(result.output),
                    exit_code=result.exit_code,
                )

    async def exec(self, argv: list[str], actor: Actor) -> ExecResult:
        result: ExecResult | None = None
        async for item in self.stream(argv, actor):
            if isinstance(item, ExecResult):
                result = item
        assert result is not None
        return result


class WorkspaceManager:
    def __init__(
        self,
        root: Path,
        audit: AuditSink,
        *,
        max_read_bytes: int = 20_000,
        max_write_bytes: int = 200_000,
        exec_timeout_s: float = 60.0,
        max_output_bytes: int = 64_000,
    ) -> None:
        self.base = Path(os.path.realpath(root.expanduser()))
        self.audit = audit
        self.max_read_bytes, self.max_write_bytes = max_read_bytes, max_write_bytes
        self.exec_timeout_s, self.max_output_bytes = exec_timeout_s, max_output_bytes

    def for_tenant(self, tenant_id: str) -> Workspace:
        return Workspace(self, tenant_id)
