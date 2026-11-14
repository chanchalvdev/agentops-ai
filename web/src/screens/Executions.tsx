import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Mono, Spinner, Stat, cx, useLoad } from "../components/ui";
import { api } from "../lib/api";
import { ms, num, when } from "../lib/format";
import { navigate } from "../lib/nav";
import type { TraceEvent } from "../lib/types";

function Timeline({ events }: { events: TraceEvent[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const max = Math.max(1, ...events.map((e) => e.latency_ms ?? 0));
  return (
    <ol className="space-y-0.5 text-xs">
      {events.map((e) => (
        <li key={e.seq}>
          <button
            className="grid w-full grid-cols-[2rem_6.5rem_minmax(0,14rem)_1fr_5rem] items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
            onClick={() => setOpen(open === e.seq ? null : e.seq)}
          >
            <span className="text-right tabular-nums text-slate-400">{e.seq}</span>
            <Badge tone={e.status === "ok" ? "gray" : e.status === "pending" ? "amber" : "red"}>{e.kind}</Badge>
            <span className={cx("truncate font-mono", e.status !== "ok" && e.status !== "pending" && "text-rose-600")}>
              {e.name}
              {e.data?.cache === "hit" ? " ⚡" : ""}
              {e.error_code ? ` · ${e.error_code}` : ""}
            </span>
            <span className="h-2 rounded bg-slate-100 dark:bg-slate-800">
              {e.latency_ms ? (
                <span
                  className={cx("block h-2 rounded", e.kind === "model_call" ? "bg-indigo-400" : "bg-emerald-400")}
                  style={{ width: `${Math.max(2, ((e.latency_ms ?? 0) / max) * 100)}%` }}
                />
              ) : null}
            </span>
            <span className="text-right tabular-nums text-slate-500">
              {e.latency_ms != null ? ms(e.latency_ms) : ""}
            </span>
          </button>
          {open === e.seq && (
            <div className="ml-10 mt-1">
              {e.message && <p className="mb-1 text-slate-600 dark:text-slate-400">{e.message}</p>}
              {e.input_tokens != null && (
                <p className="mb-1 text-slate-500">
                  tokens {e.input_tokens} in / {e.output_tokens ?? 0} out
                </p>
              )}
              <Mono>{JSON.stringify(e.data, null, 2)}</Mono>
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

function Detail({ id }: { id: string }) {
  const exec = useLoad(() => api.execution(id), [id]);
  const trace = useLoad(() => api.trace(id), [id]);
  const e = exec.data;
  return (
    <div className="space-y-4">
      <ErrorBox error={exec.error ?? trace.error} />
      {e && (
        <Card
          title={<span className="font-normal">{e.question}</span>}
          actions={
            <>
              <Badge tone="gray">{e.mode}</Badge>
              <Badge>{e.status}</Badge>
            </>
          }
        >
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
              <Stat label="Time" value={ms(e.total_ms)} />
              <Stat label="Model calls" value={e.model_calls} />
              <Stat label="Tool calls" value={e.tool_calls} />
              <Stat label="Tokens" value={num(e.input_tokens + e.output_tokens)} hint={`${e.input_tokens} in / ${e.output_tokens} out`} />
              <Stat label="Cost" value={e.cost_usd == null ? "n/a" : `$${Number(e.cost_usd).toFixed(4)}`} hint="Local models cost $0" />
            </div>
            {e.answer && <p className="whitespace-pre-wrap text-sm">{e.answer}</p>}
            {e.error_code && <p className="text-sm text-rose-600">error: {e.error_code}</p>}
            <div className="text-xs text-slate-500">
              {when(e.created_at)} · execution <span className="font-mono">{e.id}</span>
            </div>
          </div>
        </Card>
      )}
      <Card title="Timeline (execution ledger)">
        {trace.loading && !trace.data ? <Spinner /> : trace.data && <Timeline events={trace.data} />}
      </Card>
    </div>
  );
}

export default function Executions({ selected }: { selected?: string }) {
  const list = useLoad(() => api.executions(50));
  return (
    <div className="grid gap-4 xl:grid-cols-[24rem_1fr]">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">Executions</h1>
          <Button onClick={() => void list.reload()}>{list.loading ? <Spinner /> : null} Refresh</Button>
        </div>
        <ErrorBox error={list.error} />
        {list.data && list.data.length === 0 && <Empty>No runs yet. Ask something in Chat.</Empty>}
        <ul className="space-y-1">
          {list.data?.map((e) => (
            <li key={e.id}>
              <button
                onClick={() => navigate("executions", e.id)}
                className={cx(
                  "w-full rounded-md border px-3 py-2 text-left text-sm",
                  selected === e.id
                    ? "border-indigo-300 bg-indigo-50 dark:border-indigo-800 dark:bg-indigo-950"
                    : "border-slate-200 bg-white hover:bg-slate-50 dark:border-slate-800 dark:bg-slate-900 dark:hover:bg-slate-800",
                )}
              >
                <div className="truncate">{e.question}</div>
                <div className="mt-1 flex items-center gap-2 text-xs text-slate-500">
                  <Badge>{e.status}</Badge>
                  <span>{e.mode}</span>
                  <span>{ms(e.total_ms)}</span>
                  <span className="ml-auto">{when(e.created_at)}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div>{selected ? <Detail id={selected} /> : <Empty>Select an execution to see its timeline.</Empty>}</div>
    </div>
  );
}
