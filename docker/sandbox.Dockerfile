# Image that workspace commands run in when AGENTOPS_WORKSPACE_EXEC_BACKEND=docker.
# Only what the allowlisted commands need: coreutils/grep/find (in the base image), git, pytest.
# Build: docker build -t agentops-sandbox:1 -f docker/sandbox.Dockerfile docker
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "pytest==8.3.5" \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandbox
USER 10001
WORKDIR /workspace
