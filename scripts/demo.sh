#!/usr/bin/env bash
# End-to-end demo of AgentOps AI through the public API only (the same calls the web UI makes).
#
#   docker compose up -d db && uv run alembic upgrade head
#   uv run uvicorn --factory app.main:create_app --port 8000 &
#   scripts/demo.sh                       # stops at the approval and tells you how to decide
#   APPROVE=1 scripts/demo.sh             # approves the workspace write itself
#
# Env: AGENTOPS_URL (default http://localhost:8000), AGENTOPS_KEY (default dev-key-tenant-a).
# Needs curl and python3. Each agent step runs local models and takes ~20-90 s on an M4.
set -euo pipefail

URL="${AGENTOPS_URL:-http://localhost:8000}"
KEY="${AGENTOPS_KEY:-dev-key-tenant-a}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
H=(-H "X-API-Key: $KEY")

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
post() { curl -sS --fail-with-body "${H[@]}" -H 'content-type: application/json' "$URL$1" -d "$2"; }
get() { curl -sS --fail-with-body "${H[@]}" "$URL$1"; }

step "1. Health and readiness (database, Ollama, installed models)"
curl -sS "$URL/ready" | json 'd'

step "2. Upload the sample corpus (idempotent: identical content is recognised)"
for f in "$ROOT"/datasets/corpus/*.md; do
  name=$(basename "$f")
  [[ "$name" == vendor-notice-* ]] && continue  # the prompt-injection fixture is for evals only
  curl -sS "${H[@]}" -F "file=@$f" "$URL/api/v1/documents" | json "f'  {d[\"document\"][\"filename\"]}: {d[\"document\"][\"status\"]}' + (' (already uploaded)' if d['duplicate'] else '')"
done
for _ in $(seq 1 60); do
  busy=$(get "/api/v1/documents?limit=100" | json "sum(x['status'] in ('pending','processing') for x in d['items'])")
  [[ "$busy" == 0 ]] && break
  sleep 1
done
get "/api/v1/documents?limit=100" | json "'\n'.join(f'  {x[\"filename\"]} v{x[\"version\"]} {x[\"status\"]} chunks={x[\"chunk_count\"]}' for x in d['items'])"

step "3. Grounded question through the LangGraph workflow (verified, cited)"
run=$(post /api/v1/agent/run '{"question": "What is the RPO in the latest payments architecture, in seconds?"}')
echo "$run" | json "f'  [{d[\"status\"]}] {d[\"answer\"]}\n  verification={d[\"verification\"]} model_calls={d[\"model_calls\"]} tool_calls={d[\"tool_calls\"]} time={d[\"total_ms\"]/1000:.1f}s'"
exec_id=$(echo "$run" | json 'd["execution_id"]')

step "4. Open the first citation's source passage"
cite=$(echo "$run" | json 'd["citations"][0]["document_id"] + "/chunks/" + d["citations"][0]["chunk_id"] if d["citations"] else ""')
if [[ -n "$cite" ]]; then
  get "/api/v1/documents/$cite" | json "f'  {d[\"filename\"]} § {d[\"section\"]}\n  ' + d['text'][:300].replace(chr(10), ' ')"
else
  echo "  (no document citation: the answer cited tool results only)"
fi

step "5. Trace of that run (the execution ledger)"
get "/api/v1/executions/$exec_id/trace" | json "'\n'.join(f'  {e[\"seq\"]:>2} {e[\"kind\"]:<10} {e[\"name\"]:<24} {e[\"status\"]:<8} {(e[\"latency_ms\"] or 0)/1000:6.1f}s' for e in d)"

step "6. Ask the agent to change a workspace file (pauses for a human)"
run=$(post /api/v1/agent/run '{"question": "Create a new file notes/demo.md in my workspace containing the line: written by the AgentOps demo"}')
echo "$run" | json "f'  [{d[\"status\"]}] {d[\"answer\"]}'"
approval=$(get "/api/v1/approvals?status=pending" | json "next((a['id'] for a in d if a['execution_id'] == '$(echo "$run" | json 'd["execution_id"]')'), '')")
if [[ -z "$approval" ]]; then
  echo "  No approval was requested (the model may have answered without proposing a write)."
else
  get "/api/v1/approvals?status=pending" | json "'\n'.join(f'  pending {a[\"id\"]}: {a[\"tool_name\"]} {a[\"arguments\"]}' for a in d if a['id'] == '$approval')"
  if [[ "${APPROVE:-0}" == 1 ]]; then
    step "7. Approve: the run resumes from its Postgres checkpoint and performs the write"
    post "/api/v1/approvals/$approval/approve" '{"reason": "demo"}' | json "f'  [{d[\"status\"]}] {d[\"answer\"]}'"
    get "/api/v1/workspace/file?path=notes/demo.md" | json "'  notes/demo.md: ' + d['content'].strip()"
  else
    echo "  Nothing is written until a human decides. Approve or reject it in the UI (Approvals),"
    echo "  or: curl -X POST -H 'X-API-Key: $KEY' $URL/api/v1/approvals/$approval/approve"
  fi
fi

step "8. The sandbox refuses what is not allowed (no shell, path jail)"
for line in 'rm -rf calc' 'cat ../../.env' 'ls | wc -l'; do
  out=$(curl -sS "${H[@]}" -H 'content-type: application/json' "$URL/api/v1/workspace/exec" -d "{\"line\": \"$line\"}")
  echo "  \$ $line  ->  $(echo "$out" | json 'd.get("error", {}).get("code") or "exit " + str(d.get("exit_code"))')"
done

step "9. Tenant isolation: tenant B cannot read tenant A's execution"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H 'X-API-Key: dev-key-tenant-b' "$URL/api/v1/executions/$exec_id")
echo "  GET /executions/$exec_id as tenant_b -> HTTP $code"

step "10. Drift check (operator): compares current state with the committed baselines"
post /api/v1/monitoring/check '{}' | json "f'  status: {d[\"report\"][\"status\"]}  alerts: {len(d[\"alerts\"])}\n' + '\n'.join(f'  {s[\"category\"]:<12}{s[\"metric\"]:<30}{s[\"status\"]}' for s in d['report']['signals'] if s['status'] != 'ok')"

step "Done. Open the web UI (cd web && npm run dev) to see the same runs, traces, and approvals."
