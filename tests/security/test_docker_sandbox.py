"""The docker exec backend: commands run in a locked-down throwaway container.

Skipped unless Docker is running and the sandbox image exists
(docker build -t agentops-sandbox:1 -f docker/sandbox.Dockerfile docker).
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.workspace.executor import DockerSandbox, ExecResult, plan_command, run_streaming

IMAGE = "agentops-sandbox:1"


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", IMAGE],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=20,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _image_present(), reason=f"docker image {IMAGE} not built")


async def _run(root: Path, argv: list[str], timeout_s: float = 60) -> ExecResult:
    plan = plan_command(argv, lambda p, write: root / p)
    result = None
    async for item in run_streaming(
        plan,
        cwd=root,
        env={},
        timeout_s=timeout_s,
        max_output_bytes=64_000,
        sandbox=DockerSandbox(image=IMAGE),
    ):
        if isinstance(item, ExecResult):
            result = item
    assert result is not None
    return result


async def test_pytest_runs_inside_the_container_against_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    result = await _run(tmp_path, ["python", "-m", "pytest", "-q", "test_ok.py"])
    assert result.exit_code == 0, result.output
    assert "1 passed" in result.output


async def test_workspace_code_has_no_network_no_host_files_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOPS_FAKE_SECRET", "do-not-leak")
    (tmp_path / "test_escape.py").write_text(
        "import os, socket, pathlib\n"
        "def test_network():\n"
        "    s = socket.socket(); s.settimeout(2)\n"
        "    try:\n"
        "        s.connect(('1.1.1.1', 53)); reached = True\n"
        "    except OSError:\n"
        "        reached = False\n"
        "    assert not reached\n"
        "def test_no_secrets():\n"
        "    assert 'do-not-leak' not in str(os.environ)\n"
        "def test_no_host_home():\n"
        "    assert not pathlib.Path('/Users').exists()\n"
        "def test_root_is_read_only():\n"
        "    try:\n"
        "        open('/etc/pwned', 'w'); wrote = True\n"
        "    except OSError:\n"
        "        wrote = False\n"
        "    assert not wrote\n"
        "def test_not_root():\n"
        "    assert os.getuid() != 0\n"
    )
    result = await _run(tmp_path, ["python", "-m", "pytest", "-q", "test_escape.py"])
    assert result.exit_code == 0, result.output
    assert "5 passed" in result.output


async def test_a_timed_out_command_leaves_no_container_behind(tmp_path: Path) -> None:
    (tmp_path / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(60)\n")
    result = await _run(tmp_path, ["python", "-m", "pytest", "-q", "test_slow.py"], timeout_s=8)
    assert result.timed_out and result.exit_code is None
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", "name=agentops-exec-", stdout=asyncio.subprocess.PIPE
    )
    left, _ = await proc.communicate()
    assert left.strip() == b""
