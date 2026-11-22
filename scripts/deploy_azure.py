"""Deploy the AgentOps AI API to Azure Container Apps (consumption plan, scale to zero).

    uv run python scripts/deploy_azure.py                 # build, push, deploy
    uv run python scripts/deploy_azure.py --skip-build    # redeploy settings / last image

Steps:
  1. build deploy/container/Dockerfile for linux/amd64 and push it to GitHub Container Registry
     (ghcr.io/<github-user>/agentops-ai). The package must be public so Azure can pull it
     without credentials; the script checks and says how if it is not;
  2. create the resource group and a Container Apps environment (logs off: Log Analytics bills
     per GB) if they do not exist;
  3. create or update the app: 1 vCPU / 2 GiB, 0..1 replicas, external HTTPS ingress on port
     8000. Values from the env file whose names contain KEY/SECRET/TOKEN/PASSWORD/DATABASE_URL
     become Container Apps secrets; the rest become plain environment variables.

Prerequisites: `az login`, `gh auth refresh -s write:packages`, Docker running, and the env file
(.env.hosted, see .env.hosted.example). Secret values are never printed.
"""

# An operator script: it runs fixed CLIs (az, gh, docker, git) from PATH and opens fixed
# https://ghcr.io URLs. Nothing it executes comes from untrusted input.
# ruff: noqa: S310, S603, S607

import argparse
import json
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "deploy" / "container" / "Dockerfile"
SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL")
REQUIRED = ("AGENTOPS_DATABASE_URL", "AGENTOPS_LLM_API_KEY", "AGENTOPS_API_KEYS")
PORT = 8000


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.split(" #", 1)[0].strip().strip("'\"")
        if key.startswith("AGENTOPS_") and value:
            values[key] = value
    return values


def is_secret(key: str) -> bool:
    return any(marker in key for marker in SECRET_MARKERS)


def secret_name(key: str) -> str:
    """AGENTOPS_LLM_API_KEY -> llm-api-key (Container Apps: lowercase, digits and '-')."""
    return re.sub(r"[^a-z0-9-]", "-", key.removeprefix("AGENTOPS_").lower())


def run(cmd: list[str], *, capture: bool = False, check: bool = True, quiet: bool = False) -> str:
    """Run a command. `quiet` hides the arguments (they contain secrets) from the log."""
    print("$", " ".join(cmd[:4]) + (" …" if quiet or len(cmd) > 4 else ""), flush=True)
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture, check=False)
    if check and result.returncode != 0:
        if capture:
            sys.stderr.write(result.stderr[-2000:])
        raise SystemExit(f"command failed ({result.returncode}): {cmd[0]} {cmd[1]}")
    return (result.stdout or "").strip()


def az_exists(*args: str) -> bool:
    return subprocess.run(["az", *args], capture_output=True, check=False).returncode == 0


def ghcr_public(repo: str) -> bool:
    """True if anyone can pull ghcr.io/<repo> (anonymous token + tags request succeed).

    Uses certifi's CA bundle: python.org builds on macOS ship without system root certificates,
    and a certificate failure must not read as "private".
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        url = f"https://ghcr.io/token?scope=repository:{repo}:pull"
        with urllib.request.urlopen(url, context=ctx) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{repo}/tags/list", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, context=ctx) as r:
            return bool(r.status == 200)
    except (urllib.error.URLError, KeyError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.hosted")
    parser.add_argument("--app", default="agentops-api")
    parser.add_argument("--resource-group", default="agentops-rg")
    parser.add_argument("--environment", default="agentops-env")
    parser.add_argument("--location", default="centralindia")
    parser.add_argument("--image-name", default="agentops-ai")
    parser.add_argument("--tag", help="image tag (default: git short SHA)")
    parser.add_argument("--skip-build", action="store_true", help="deploy the existing tag")
    args = parser.parse_args()

    for tool in ("az", "gh", "docker", "git"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is not installed (see docs/deployment.md)")
    if not az_exists("account", "show"):
        raise SystemExit("not logged in to Azure: run `az login`")
    if not args.env_file.is_file():
        raise SystemExit(f"missing {args.env_file}: cp .env.hosted.example .env.hosted")
    env = read_env(args.env_file)
    missing = [k for k in REQUIRED if k not in env]
    if missing:
        raise SystemExit(f"{args.env_file.name} is missing: {', '.join(missing)}")

    owner = run(["gh", "api", "user", "-q", ".login"], capture=True).lower()
    tag = args.tag or run(["git", "rev-parse", "--short", "HEAD"], capture=True)
    if not args.tag and run(["git", "status", "--porcelain"], capture=True):
        tag += "-dirty"  # uncommitted changes are in the image: say so in its name
    repo = f"{owner}/{args.image_name}"
    image = f"ghcr.io/{repo}:{tag}"

    # 1. image
    if not args.skip_build:
        token = run(["gh", "auth", "token"], capture=True)
        login = subprocess.run(
            ["docker", "login", "ghcr.io", "-u", owner, "--password-stdin"],
            input=token,
            text=True,
            capture_output=True,
            check=False,
        )
        if login.returncode != 0:
            raise SystemExit(
                "docker login to ghcr.io failed: run `gh auth refresh -s write:packages`"
            )
        run(
            [
                "docker", "buildx", "build", "--platform", "linux/amd64",
                "-f", str(DOCKERFILE), "-t", image, "--push", ".",
            ]
        )  # fmt: skip
    if not ghcr_public(repo):
        print(
            f"\nThe image is private, so Azure cannot pull it. Make it public once:\n"
            f"  https://github.com/users/{owner}/packages/container/{args.image_name}/settings\n"
            f"  -> Danger Zone -> Change visibility -> Public\n"
            f"then rerun with --skip-build --tag {tag}",
            file=sys.stderr,
        )
        return 1

    # 2. resource group + environment
    rg, envname = args.resource_group, args.environment
    run(["az", "extension", "add", "--name", "containerapp", "--upgrade", "--yes", "-o", "none"])
    run(["az", "provider", "register", "--namespace", "Microsoft.App", "--wait"])
    if not az_exists("group", "show", "-n", rg):
        run(["az", "group", "create", "-n", rg, "-l", args.location, "-o", "none"])
    if not az_exists("containerapp", "env", "show", "-n", envname, "-g", rg):
        run(
            [
                "az", "containerapp", "env", "create", "-n", envname, "-g", rg,
                "-l", args.location, "--logs-destination", "none", "-o", "none",
            ]
        )  # fmt: skip

    # 3. app
    secrets = {secret_name(k): v for k, v in env.items() if is_secret(k)}
    env_args = [
        f"{k}=secretref:{secret_name(k)}" if is_secret(k) else f"{k}={v}" for k, v in env.items()
    ]
    secret_args = [f"{name}={value}" for name, value in secrets.items()]
    exists = az_exists("containerapp", "show", "-n", args.app, "-g", rg)
    if exists:
        run(
            ["az", "containerapp", "secret", "set", "-n", args.app, "-g", rg,
             "--secrets", *secret_args, "-o", "none"],
            quiet=True,
        )  # fmt: skip
        run(
            ["az", "containerapp", "update", "-n", args.app, "-g", rg, "--image", image,
             "--min-replicas", "0", "--max-replicas", "1", "--cpu", "1", "--memory", "2Gi",
             "--set-env-vars", *env_args, "-o", "none"],
            quiet=True,
        )  # fmt: skip
    else:
        run(
            ["az", "containerapp", "create", "-n", args.app, "-g", rg, "--environment", envname,
             "--image", image, "--target-port", str(PORT), "--ingress", "external",
             "--min-replicas", "0", "--max-replicas", "1", "--cpu", "1", "--memory", "2Gi",
             "--secrets", *secret_args, "--env-vars", *env_args, "-o", "none"],
            quiet=True,
        )  # fmt: skip
    fqdn = run(
        ["az", "containerapp", "show", "-n", args.app, "-g", rg,
         "--query", "properties.configuration.ingress.fqdn", "-o", "tsv"],
        capture=True,
    )  # fmt: skip
    print(f"\ndeployed {image}")
    print(f"API: https://{fqdn}   (check: curl https://{fqdn}/ready)")
    print(f"secrets: {', '.join(sorted(secrets))}")
    if "AGENTOPS_CORS_ORIGINS" not in env:
        print("note: AGENTOPS_CORS_ORIGINS is not set; add the UI's URL and rerun --skip-build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
