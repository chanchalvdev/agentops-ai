import { ArrowUpRight, RefreshCw, Stamp } from "lucide-react";
import { useEffect, useState } from "react";
import { CustodyChain, Seal } from "../components/CustodyChain";
import { Button, Code, Empty, ErrorBox, PageHead, RecordId, Spinner, Status, cx, inputClass, recordId, selectClass, useLoad } from "../components/ui";
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
  const added = lines.filter((l) => l.kind === "add").length;
  const removed = lines.filter((l) => l.kind === "del").length;
  return (
    <div className="border-1.5 border-ink/25 bg-paper">
      <div className="flex gap-4 border-b border-rule px-3 py-1.5 font-mono text-xs">
        <span className="text-record">+{added}</span>
        <span className="text-seal">−{removed}</span>
      </div>
      <div className="max-h-[360px] overflow-auto py-1 font-mono text-xs leading-relaxed">
        {lines.map((l, i) =>
          l.kind === "gap" ? (
            <div key={i} className="bg-wash px-3 py-0.5 text-ink-3">
              ⋯ {l.count} unchanged line{l.count === 1 ? "" : "s"}
            </div>
          ) : (
            <div
              key={i}
              className={cx(
                "grid grid-cols-[1.5rem_1fr] whitespace-pre-wrap break-words px-3",
                l.kind === "add" && "bg-record-soft text-[#1E5A37]",
                l.kind === "del" && "bg-seal-soft text-[#8A1C25]",
              )}
            >
              <span aria-hidden className="select-none">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</span>
              <span>
                <span className="sr-only">{l.kind === "add" ? "added: " : l.kind === "del" ? "removed: " : ""}</span>
                {l.text || " "}
              </span>
            </div>
          ),
        )}
      </div>
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
          if (after === null) note = "The text to replace is not in the current file, so the tool will refuse this edit.";
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
    return <Code>$ {argv.join(" ")}</Code>;
  }
  if (!isWrite) return <Code>{JSON.stringify(args, null, 2)}</Code>;
  if (approval.status !== "pending")
    return <Code>{approval.tool_name === "workspace_edit" ? `− ${String(args.old)}\n+ ${String(args.new)}` : String(args.content ?? "")}</Code>;
  return (
    <div className="space-y-2">
      <ErrorBox error={state.error} />
      {state.note && <p className="text-sm font-semibold text-caution">{state.note}</p>}
      {state.before === undefined && !state.error ? (
        <Spinner />
      ) : state.after != null ? (
        <>
          {!state.exists && <p className="text-xs font-semibold text-info">New file: it does not exist yet.</p>}
          <DiffView before={state.before ?? ""} after={state.after} />
        </>
      ) : (
        <Code>{`− ${String(args.old)}\n+ ${String(args.new)}`}</Code>
      )}
    </div>
  );
}

function HeldChain({ executionId, version }: { executionId: string; version: string }) {
  // The held run's custody chain: it ends at the Approval hand, awaiting your countersignature
  // while pending, and signed by you (with the time) once decided.
  const trace = useLoad(() => api.trace(executionId), [executionId, version]);
  if (trace.error || !trace.data) return null;
  return (
    <div className="-mx-5 border-b border-rule">
      <CustodyChain events={trace.data} compact />
    </div>
  );
}

function ApprovalRecord({ approval, onDecided }: { approval: Approval; onDecided: (r: AgentRunResult) => void }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const path = approval.arguments.path;
  const pending = approval.status === "pending";

  async function decide(approve: boolean) {
    setBusy(approve ? "approve" : "reject");
    setError(null);
    try {
      onDecided(await api.decide(approval.id, approve, reason));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className={cx("border-1.5 bg-ground", pending ? "border-ink" : "border-rule")}>
      <header className="flex flex-wrap items-center gap-3 border-b border-rule px-5 py-3">
        <RecordId prefix="AP" id={approval.id} invert={pending} />
        <span className="font-mono text-sm font-bold">{approval.tool_name}</span>
        {typeof path === "string" && <span className="min-w-0 truncate font-mono text-sm text-ink-2">{path}</span>}
        <span className="ml-auto">
          <Status value={approval.status} />
        </span>
      </header>
      <div className="space-y-4 px-5 pb-4">
        <HeldChain executionId={approval.execution_id} version={approval.status} />
        <p className="text-xs text-ink-2">
          Requested <span className="font-mono">{when(approval.created_at)}</span> by run{" "}
          <button className="font-mono font-semibold text-stamp underline decoration-stamp/40 hover:decoration-stamp" onClick={() => navigate("executions", approval.execution_id)}>
            {recordId("EX", approval.execution_id)}
          </button>
          {approval.decided_at && (
            <>
              {" · decided "}
              <span className="font-mono">{when(approval.decided_at)}</span>
            </>
          )}
          {approval.reason && ` · reason: ${approval.reason}`}
        </p>
        <Preview approval={approval} />
        <ErrorBox error={error} />
      </div>
      {pending && (
        <footer className="flex flex-wrap items-center gap-3 border-t-1.5 border-ink bg-wash px-5 py-3">
          <label className="min-w-64 flex-1">
            <span className="sr-only">Reason, recorded with the decision</span>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={1000}
              placeholder="Reason for the record (optional)"
              className={inputClass}
            />
          </label>
          <Button variant="danger" busy={busy === "reject"} disabled={busy !== null} onClick={() => void decide(false)}>
            Reject
          </Button>
          <Button variant="seal" busy={busy === "approve"} disabled={busy !== null} onClick={() => void decide(true)}>
            <Stamp className="h-4 w-4" aria-hidden /> Countersign and apply
          </Button>
        </footer>
      )}
    </article>
  );
}

export default function Approvals() {
  const [status, setStatus] = useState<"pending" | "all">("pending");
  const list = useLoad(() => api.approvals(status), [status]);
  const [last, setLast] = useState<AgentRunResult | null>(null);

  return (
    <div className="mx-auto max-w-5xl px-8 py-8">
      <PageHead
        title="Approvals"
        sub="Writes and side-effecting commands the agent proposed. Nothing changes until you countersign; the run then resumes from its checkpoint."
        actions={
          <>
            <label className="sr-only" htmlFor="approval-filter">
              Show
            </label>
            <select id="approval-filter" value={status} onChange={(e) => setStatus(e.target.value as "pending" | "all")} className={selectClass}>
              <option value="pending">Pending</option>
              <option value="all">All decisions</option>
            </select>
            <Button variant="ghost" onClick={() => void list.reload()} busy={list.loading && !!list.data}>
              <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Refresh
            </Button>
          </>
        }
      />
      {last && (
        <section className="mb-8 flex flex-wrap items-center gap-5 border-1.5 border-ink bg-paper px-5 py-4" aria-live="polite">
          <Seal status={last.status} verification={last.verification} totalMs={last.total_ms} />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold">
              Run {recordId("EX", last.execution_id)} resumed and sealed
            </p>
            <p className="mt-1 text-md">{last.answer}</p>
          </div>
          <Button size="sm" variant="ghost" onClick={() => navigate("executions", last.execution_id)}>
            Ledger <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
          </Button>
        </section>
      )}
      <ErrorBox error={list.error} />
      {!list.data && list.loading && <Spinner />}
      {list.data && list.data.length === 0 && (
        <Empty title={status === "pending" ? "Nothing waits for you" : "No decisions on record"}>
          When the agent proposes a file change or a command that needs approval, it appears here with a diff.
        </Empty>
      )}
      <div className="space-y-6">
        {list.data?.map((a) => (
          <ApprovalRecord
            key={a.id}
            approval={a}
            onDecided={(r) => {
              setLast(r);
              window.dispatchEvent(new Event("agentops:approvals"));
              void list.reload();
            }}
          />
        ))}
      </div>
    </div>
  );
}
