"""Allowlisted command execution for the workspace (product.txt 9.1).

No shell is ever involved: argv goes straight to exec, so pipes, globbing, redirection, and
command substitution do not exist. Each allowed command has a rule listing the options it may
use. Anything unlisted is rejected, because options are where "read-only" programs hide writes
and escapes: `grep -f /etc/passwd`, `find -delete`, `find -exec`, `git diff --output=FILE`.
Positional arguments are treated as paths and go through the same jail as the file tools.

Two backends run the planned argv (the allowlist and path jail apply to both):
- "host": a child process with a scrubbed environment. Not an OS sandbox: an approved
  `python -m pytest` runs workspace code with the server's privileges; approval is the control.
- "docker": a throwaway container per command (DockerSandbox): no network, all capabilities
  dropped, read-only root filesystem, CPU/memory/PID limits, non-root user, and only the
  tenant's directory mounted. Workspace code then cannot reach the server, its files, its
  credentials, or the network even when a human approved running it.
"""

import asyncio
import codecs
import contextlib
import os
import re
import shutil
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.core.errors import CommandNotAllowedError, ToolExecutionError

SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
PYTHON = "<python>"  # replaced by the server's own interpreter
_NUMERIC_FLAG = re.compile(r"^-\d{1,6}$")
_GIT_HARDENING = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
_MAX_ARGS, _MAX_ARG_CHARS, _MAX_VALUE_CHARS = 32, 1000, 200


@dataclass(frozen=True)
class CommandRule:
    name: str  # what the user types, e.g. "git status"
    run_as: tuple[str, ...]  # what is executed in its place (program resolved on PATH)
    writes: bool  # may change the workspace or run its code -> needs approval for the agent
    flags: frozenset[str] = frozenset()  # options without a value
    value_flags: frozenset[str] = frozenset()  # options taking a value: "-n 5" or "--tb=short"
    combined_short: bool = True  # "-la" means "-l -a"
    numeric_flag: bool = False  # "-5" (head, git log)
    pattern_first: bool = False  # grep: the first positional is a pattern, not a path
    paths_are_writes: bool = False  # mkdir: positionals are created, so write rules apply
    inject: tuple[str, ...] = ()  # hardening options always added before the user's arguments

    @property
    def prefix(self) -> tuple[str, ...]:
        return tuple(self.name.split())


def _rule(name: str, *, flags: str = "", values: str = "", **kw: object) -> CommandRule:
    run_as = kw.pop("run_as", tuple(name.split()))
    return CommandRule(
        name=name,
        run_as=run_as,  # type: ignore[arg-type]
        flags=frozenset(flags.split()),
        value_flags=frozenset(values.split()),
        **kw,  # type: ignore[arg-type]
    )


_PYTEST = (PYTHON, "-m", "pytest")
# --rootdir/-c keep pytest from picking up config (and conftest.py) above the workspace.
_PYTEST_INJECT = ("-p", "no:cacheprovider", "--rootdir=.", "-c", os.devnull)
_PYTEST_FLAGS = "-q -v -x -s -l --co --collect-only --no-header -rA"
_PYTEST_VALUES = "-k --tb --maxfail -m"

RULES: tuple[CommandRule, ...] = (
    # read-only
    _rule("ls", flags="-l -a -A -h -R -1 -t -S -r -F", writes=False),
    _rule("cat", flags="-n", writes=False),
    _rule("head", values="-n -c", writes=False, numeric_flag=True),
    _rule("tail", values="-n -c", writes=False, numeric_flag=True),
    _rule("wc", flags="-l -w -c -m", writes=False),
    _rule(
        "grep",
        flags="-r -n -i -l -L -c -E -F -w -v -H -h -o -s -I",  # no -R: GNU grep follows symlinks
        values="-m -A -B -C --include --exclude --exclude-dir",
        writes=False,
        pattern_first=True,
    ),
    _rule(
        "find",
        flags="-print",
        values="-name -iname -path -type -maxdepth -mindepth",
        writes=False,
        combined_short=False,
    ),
    _rule(
        "git status",
        flags="-s --short --porcelain -b --branch",
        writes=False,
        run_as=("git", *_GIT_HARDENING, "status"),
    ),
    _rule(
        "git diff",
        flags="--stat --cached --staged --name-only --name-status",
        writes=False,
        run_as=("git", *_GIT_HARDENING, "diff"),
        inject=("--no-ext-diff", "--no-textconv", "--no-color"),
    ),
    _rule(
        "git log",
        flags="--oneline --stat",
        values="-n --max-count",
        writes=False,
        numeric_flag=True,
        run_as=("git", *_GIT_HARDENING, "log"),
        inject=("--no-ext-diff", "--no-textconv", "--no-color"),
    ),
    # write / execute: the agent needs a human approval for these
    # pytest is here even with --collect-only: collection imports test modules and conftest.py,
    # which runs their code (product.txt lists it as read-only; that would be unsafe).
    _rule(
        "python -m pytest",
        flags=_PYTEST_FLAGS,
        values=_PYTEST_VALUES,
        writes=True,
        run_as=_PYTEST,
        inject=_PYTEST_INJECT,
    ),
    _rule(
        "pytest",
        flags=_PYTEST_FLAGS,
        values=_PYTEST_VALUES,
        writes=True,
        run_as=_PYTEST,
        inject=_PYTEST_INJECT,
    ),
    _rule("git add", flags="-v -n --dry-run", writes=True, run_as=("git", *_GIT_HARDENING, "add")),
    _rule("mkdir", flags="-p -v", writes=True, paths_are_writes=True),
)
ALLOWED = ", ".join(r.name for r in RULES)


class ExecResult(BaseModel):
    argv: list[str]
    exit_code: int | None  # None when the process was killed (timeout or output cap)
    output: str  # stdout and stderr interleaved, decoded as UTF-8 (invalid bytes replaced)
    truncated: bool
    timed_out: bool
    duration_ms: float


@dataclass(frozen=True)
class CommandPlan:
    rule: CommandRule
    argv: list[str]  # as requested
    exec_argv: list[str]  # what will actually run

    @property
    def writes(self) -> bool:
        return self.rule.writes


def _match(argv: list[str]) -> CommandRule:
    candidates = [r for r in RULES if tuple(argv[: len(r.prefix)]) == r.prefix]
    if not candidates:
        shown = " ".join(argv[:2])[:60]
        raise CommandNotAllowedError(f"Command not allowed: {shown}. Allowed: {ALLOWED}")
    return max(candidates, key=lambda r: len(r.prefix))


def _check_value(value: str) -> None:
    if len(value) > _MAX_VALUE_CHARS:
        raise CommandNotAllowedError("Option value too long")


def plan_command(argv: list[str], resolve: Callable[[str, bool], Path]) -> CommandPlan:
    """Validate argv against the rules; `resolve(path, write)` is the workspace path jail."""
    if not argv or len(argv) > _MAX_ARGS:
        raise CommandNotAllowedError(f"Commands take 1 to {_MAX_ARGS} arguments")
    if any(not a or "\x00" in a or len(a) > _MAX_ARG_CHARS for a in argv):
        raise CommandNotAllowedError("Empty, oversized, or NUL-containing argument")
    rule = _match(argv)
    rest = argv[len(rule.prefix) :]
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            positionals.extend(rest[i + 1 :])
            break
        if rule.numeric_flag and _NUMERIC_FLAG.match(arg):
            pass
        elif arg.startswith("--"):
            key, eq, value = arg.partition("=")
            if key in rule.value_flags:
                if not eq:
                    i += 1
                    if i == len(rest):
                        raise CommandNotAllowedError(f"Option {key} needs a value")
                    value = rest[i]
                _check_value(value)
            elif key not in rule.flags or eq:
                raise CommandNotAllowedError(f"Option not allowed for {rule.name}: {key[:40]}")
        elif arg.startswith("-") and arg != "-":
            if arg in rule.value_flags:
                i += 1
                if i == len(rest):
                    raise CommandNotAllowedError(f"Option {arg} needs a value")
                _check_value(rest[i])
            elif arg not in rule.flags and not (
                rule.combined_short and len(arg) > 2 and all(f"-{c}" in rule.flags for c in arg[1:])
            ):
                raise CommandNotAllowedError(f"Option not allowed for {rule.name}: {arg[:40]}")
        else:
            positionals.append(arg)
        i += 1

    paths = positionals[1:] if rule.pattern_first else positionals
    if rule.pattern_first and positionals:
        _check_value(positionals[0])
    for path in paths:
        resolve(path, rule.paths_are_writes)  # raises WorkspaceAccessError outside the jail

    program, *fixed = rule.run_as
    if program == PYTHON:
        executable = sys.executable
    else:
        found = shutil.which(program, path=SAFE_PATH)
        if found is None:
            raise CommandNotAllowedError(f"{program} is not installed on the server")
        executable = found
    return CommandPlan(rule, list(argv), [executable, *fixed, *rule.inject, *rest])


def scrubbed_env(home: Path, ceiling: Path) -> dict[str, str]:
    """The complete environment of a workspace command. Nothing is inherited from the server,
    so API keys, database URLs, and cloud credentials in its environment never leak."""
    return {
        "PATH": SAFE_PATH,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        # git must not walk up out of the tenant directory into an enclosing repository.
        "GIT_CEILING_DIRECTORIES": str(ceiling),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: pytest may spawn children


@dataclass(frozen=True)
class DockerSandbox:
    """Runs each command in a fresh container. The docker CLI itself runs with the server's
    environment (it needs its socket/config); the container gets only `env` below."""

    image: str = "agentops-sandbox:1"
    cpus: str = "1"
    memory: str = "512m"
    pids: int = 128
    docker: str = "docker"

    def argv(self, plan: CommandPlan, cwd: Path, name: str) -> list[str]:
        program = plan.rule.run_as[0]
        inner = ["python" if program == PYTHON else program, *plan.exec_argv[1:]]
        env = {
            # Paths inside the container (its private tmpfs), not on the host.
            **scrubbed_env(Path("/tmp/home"), Path("/")),  # noqa: S108
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        return [
            self.docker,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",  # noqa: S108 - the container's own tmpfs
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids),
            "--user",
            "10001:10001",
            "--volume",
            f"{cwd}:/workspace:rw",
            "--workdir",
            "/workspace",
            *(arg for key, value in env.items() for arg in ("--env", f"{key}={value}")),
            self.image,
            *inner,
        ]

    async def remove(self, name: str) -> None:
        """Killing the CLI does not stop the container: remove it explicitly."""
        with contextlib.suppress(OSError):
            proc = await asyncio.create_subprocess_exec(
                self.docker,
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()


async def run_streaming(
    plan: CommandPlan,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    max_output_bytes: int,
    sandbox: DockerSandbox | None = None,
) -> AsyncIterator[str | ExecResult]:
    """Yield output text as it arrives, then one ExecResult. Closing the iterator early (client
    gone, caller timeout) kills the process group (and removes the container)."""
    started = time.perf_counter()
    container = f"agentops-exec-{uuid.uuid4().hex[:12]}" if sandbox else None
    argv = sandbox.argv(plan, cwd, container) if sandbox and container else plan.exec_argv
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={**os.environ} if sandbox else env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise ToolExecutionError(f"Could not start {plan.rule.name}", detail=repr(exc)) from exc
    assert proc.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    parts: list[str] = []
    size, truncated, timed_out = 0, False, False
    try:
        async with asyncio.timeout(timeout_s):
            while chunk := await proc.stdout.read(4096):
                keep = chunk[: max_output_bytes - size]
                size += len(keep)
                if keep:
                    text = decoder.decode(keep)
                    parts.append(text)
                    yield text
                if size >= max_output_bytes:
                    truncated = True
                    break
            if not truncated:
                await proc.wait()
    except TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            _kill(proc)
            await proc.wait()
            if sandbox and container:
                await sandbox.remove(container)
    tail = decoder.decode(b"", final=True)
    if tail:
        parts.append(tail)
        yield tail
    yield ExecResult(
        argv=plan.argv,
        exit_code=None if (timed_out or truncated) else proc.returncode,
        output="".join(parts),
        truncated=truncated,
        timed_out=timed_out,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
