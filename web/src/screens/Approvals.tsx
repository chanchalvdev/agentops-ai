import { useEffect, useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Mono, Spinner, cx, useLoad } from "../components/ui";
import { ApiError, api } from "../lib/api";
import { applyEdit, lineDiff, withContext } from "../lib/diff";
import { when } from "../lib/format";
import { navigate } from "../lib/nav";
import type { AgentRunResult, Approval } from "../lib/types";

/** Current content of the target file, or "" when it does not exist yet (a new file). */
async function currentFile(path: string): Promise<{ content: string; exists: boolean; truncated: boolean }> {
  try {
    const f = await api.readFile(path);
    return { content: f.content, exists: true, truncated: f.truncated };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { content: "", exists: false, truncated: false };
    throw e;
  }
}

function DiffView({ before, after }: { before: string; after: string }) {
  const lines = withContext(lineDiff(before, after));
  return (
    <div className="overflow-auto rounded-md border border-slate-200 font-mono text-xs dark:border-slate-800">
      {lines.map((l, i) =>
        l.kind === "gap" ? (
          <div key={i} className="bg-slate-50 px-3 py-0.5 text-slate-400 dark:bg-slate-950">⋯ {l.count} unchanged lines</div>
        ) : (
          <div
            key={i}
            className={cx(
              "whitespace-pre-wrap px-3",
              l.kind === "add" && "bg-emerald-50 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300",
              l.kind === "del" && "bg-rose-50 text-rose-900 dark:bg-rose-950/40 dark:text-rose-300",
            )}
          >
            {l.kind === "add" ? "+ " : l.kind === "del" ? "- " : "  "}
            {l.text}
          </div>
        ),
      )}
    </div>
  );
}

function Preview({ approval }: { approval: Approval }) {
  const args = approval.arguments;
  const path = typeof args.path === "string" ? args.path : null;
  const isWrite = approval.tool_name === "workspace_write" || approval.tool_name === "workspace_edit";
  const [state, setState] = useState<{ before?: string; after?: string | null; exists?: boolean; note?: string; error?: unknown }>({});

  useEffect(() => {
    if (!isWrite || !path || approval.status !== "pending") return;
    let alive = true;
    void (async () => {
      try {
        const cur = await currentFile(path);
        let after: string | null;
        let note: string | undefined;
        if (approval.tool_name === "workspace_write") after = String(args.content ?? "");
        else {
          after = applyEdit(cur.content, String(args.old ?? ""), String(args.new ?? ""));
          if (after === null) note = "The text to replace is not in the current file; the tool will refuse this edit.";
        }
        if (cur.truncated) note = "The file is larger than the preview limit; the diff covers its first part only.";
        if (alive) setState({ before: cur.content, after, exists: cur.exists, note });
      } catch (error) {
        if (alive) setState({ error });
      }
    })();
    return () => {
      alive = false;
    };
  }, [approval.id, approval.status, approval.tool_name, isWrite, path, args.content, args.old, args.new]);

  if (approval.tool_name === "workspace_exec") {
    const argv = Array.isArray(args.argv) ? (args.argv as string[]) : [];
    return <Mono>$ {argv.join(" ")}</Mono>;
  }
  if (!isWrite) return <Mono>{JSON.stringify(args, null, 2)}</Mono>;
  if (approval.status !== "pending")
    return <Mono>{approval.tool_name === "workspace_edit" ? `- ${String(args.old)}\n+ ${String(args.new)}` : String(args.content ?? "")}</Mono>;
  return (
    <div className="space-y-2">
      <ErrorBox error={state.error} />
      {state.note && <p className="text-xs text-amber-700 dark:text-amber-400">{state.note}</p>}
      {state.before === undefined && !state.error ? (
        <Spinner />
      ) : state.after != null ? (
        <>
          {!state.exists && <Badge tone="blue">new file</Badge>}
          <DiffView before={state.before ?? ""} after={state.after} />
        </>
      ) : (
        <Mono>{`- ${String(args.old)}\n+ ${String(args.new)}`}</Mono>
      )}
    </div>
  );
}

function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: (r: AgentRunResult) => void }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const path = approval.arguments.path;

  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      onDecided(await api.decide(approval.id, approve, reason));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span className="font-mono">{approval.tool_name}</span>
          {typeof path === "string" && <span className="font-mono font-normal text-slate-500">{path}</span>}
        </span>
      }
      actions={<Badge>{approval.status}</Badge>}
    >
      <div className="space-y-3">
        <div className="text-xs text-slate-500">
          requested {when(approval.created_at)} ·{" "}
          <button className="underline" onClick={() => navigate("executions", approval.execution_id)}>
            execution {approval.execution_id.slice(0, 8)}
          </button>
          {approval.decided_at && ` · decided ${when(approval.decided_at)}`}
          {approval.reason && ` · reason: ${approval.reason}`}
        </div>
        <Preview approval={approval} />
        <ErrorBox error={error} />
        {approval.status === "pending" && (
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={1000}
              placeholder="Reason (optional, recorded with the decision)"
              className="min-w-64 flex-1 rounded-md border border-slate-300 bg-transparent px-2 py-1.5 text-sm dark:border-slate-700"
            />
            <Button variant="primary" disabled={busy} onClick={() => void decide(true)}>
              {busy ? <Spinner /> : null} Approve
            </Button>
            <Button variant="danger" disabled={busy} onClick={() => void decide(false)}>
              Reject
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

export default function Approvals() {
  const [status, setStatus] = useState<"pending" | "all">("pending");
  const list = useLoad(() => api.approvals(status), [status]);
  const [last, setLast] = useState<AgentRunResult | null>(null);

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Approvals</h1>
        <div className="flex gap-2">
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as "pending" | "all")}
            className="rounded-md border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
          >
            <option value="pending">Pending</option>
            <option value="all">All</option>
          </select>
          <Button onClick={() => void list.reload()}>{list.loading ? <Spinner /> : null} Refresh</Button>
        </div>
      </div>
      <p className="text-sm text-slate-500">
        Writes and side-effecting commands the agent proposed. Nothing changes until you approve; the run then
        resumes from its checkpoint on the server.
      </p>
      {last && (
        <Card title="Run resumed">
          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <Badge>{last.status}</Badge>
              <button className="text-xs underline" onClick={() => navigate("executions", last.execution_id)}>
                open trace
              </button>
            </div>
            <p className="whitespace-pre-wrap">{last.answer}</p>
          </div>
        </Card>
      )}
      <ErrorBox error={list.error} />
      {list.data && list.data.length === 0 && <Empty>No {status === "pending" ? "pending " : ""}approvals.</Empty>}
      {list.data?.map((a) => (
        <ApprovalCard
          key={a.id}
          approval={a}
          onDecided={(r) => {
            setLast(r);
            void list.reload();
          }}
        />
      ))}
    </div>
  );
}
