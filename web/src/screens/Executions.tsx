import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Fragment, useState } from "react";
import { CustodyChain } from "../components/CustodyChain";
import { AnswerText, Exhibits, useEvidence } from "../components/Evidence";
import { Button, Code, Empty, ErrorBox, Figure, RecordId, Spinner, Status, cx, label, useLoad } from "../components/ui";
import { api } from "../lib/api";
import { ms, when } from "../lib/format";
import { navigate } from "../lib/nav";
import type { TraceEvent } from "../lib/types";

function Ledger({ events }: { events: TraceEvent[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const max = Math.max(1, ...events.map((e) => e.latency_ms ?? 0));
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-rule text-left text-xs text-ink-2">
          <th className="w-10 py-2.5 pr-2 text-right font-semibold">#</th>
          <th className="px-3 font-semibold">Kind</th>
          <th className="px-3 font-semibold">Name</th>
          <th className="px-3 font-semibold">Outcome</th>
          <th className="w-[28%] px-3 font-semibold">Latency</th>
          <th className="px-3 text-right font-semibold">Tokens</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e) => {
          const expanded = open === e.seq;
          const bad = e.status === "error" || e.status === "rejected";
          return (
            <Fragment key={e.seq}>
              <tr
                className={cx("cursor-pointer border-b border-rule/70 hover:bg-wash/70", expanded && "bg-wash")}
                onClick={() => setOpen(expanded ? null : e.seq)}
              >
                <td className="py-2 pr-2 text-right font-mono text-xs text-ink-3">{e.seq}</td>
                <td className="whitespace-nowrap px-3 text-xs font-semibold text-ink-2">{label(e.kind)}</td>
                <td className="px-3">
                  <button
                    className={cx("flex items-center gap-1 text-left font-mono text-xs", bad ? "text-seal" : "text-ink")}
                    aria-expanded={expanded}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      setOpen(expanded ? null : e.seq);
                    }}
                  >
                    {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
                    {e.name}
                    {e.kind === "model_call" && e.data?.model ? <span className="text-ink-2"> · {String(e.data.model)}</span> : null}
                    {e.data?.cache === "hit" && <span className="ml-1 text-stamp">cache hit</span>}
                  </button>
                </td>
                <td className="px-3">
                  {e.status === "ok" ? (
                    <span className="text-xs text-ink-2">ok</span>
                  ) : (
                    <Status value={e.error_code ?? e.status} tone={e.status === "pending" ? "pending" : "critical"} />
                  )}
                </td>
                <td className="px-3">
                  {e.latency_ms ? (
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 flex-1 bg-wash">
                        <div
                          className={cx("h-1.5", e.kind === "model_call" ? "bg-stamp" : "bg-ink-3")}
                          style={{ width: `${Math.max(2, ((e.latency_ms ?? 0) / max) * 100)}%` }}
                        />
                      </div>
                      <span className="w-14 text-right font-mono text-xs">{ms(e.latency_ms)}</span>
                    </div>
                  ) : (
                    <span className="text-xs text-ink-3">–</span>
                  )}
                </td>
                <td className="px-3 text-right font-mono text-xs">
                  {e.input_tokens != null ? `${e.input_tokens}+${e.output_tokens ?? 0}` : ""}
                </td>
              </tr>
              {expanded && (
                <tr className="bg-wash">
                  <td />
                  <td colSpan={5} className="px-3 pb-3">
                    {e.message && <p className="mb-2 text-sm">{e.message}</p>}
                    <Code>{JSON.stringify(e.data, null, 2)}</Code>
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

function Detail({ id }: { id: string }) {
  const exec = useLoad(() => api.execution(id), [id]);
  const trace = useLoad(() => api.trace(id), [id]);
  const e = exec.data;
  const final = trace.data?.find((t) => t.kind === "final");
  const ev = useEvidence();
  const citesTools = !!e?.answer && /\[T\d+\]/.test(e.answer);
  return (
    <div className="min-w-0">
      <ErrorBox error={exec.error ?? trace.error} />
      {!e && exec.loading && <Spinner />}
      {e && (
        <article className="animate-sign">
          <header className="flex flex-wrap items-baseline gap-x-4 gap-y-2 border-b-1.5 border-ink pb-4">
            <RecordId prefix="EX" id={e.id} invert />
            <h1 className="min-w-0 flex-1 text-2xl font-bold">{e.question}</h1>
            <span className="font-mono text-xs text-ink-2">
              {e.mode} · {when(e.created_at)}
            </span>
          </header>
          {trace.data && (
            <div className="-mx-8">
              <CustodyChain
                events={trace.data}
                final={{ status: e.status, verification: final ? String(final.data?.verification ?? "") || null : null, totalMs: e.total_ms }}
              />
            </div>
          )}
          <dl className="grid grid-cols-2 gap-6 border-b border-rule py-6 sm:grid-cols-5">
            <Figure value={ms(e.total_ms)} label="total time" />
            <Figure value={e.model_calls} label="model calls" />
            <Figure value={e.tool_calls} label="tool calls" />
            <Figure value={(e.input_tokens + e.output_tokens).toLocaleString()} label="tokens" hint={`${e.input_tokens} in · ${e.output_tokens} out`} />
            <Figure value={e.cost_usd == null ? "n/a" : `$${Number(e.cost_usd).toFixed(2)}`} label="cost (local)" />
          </dl>
          {(e.answer || e.error_code) && (
            <div className="grid gap-x-10 gap-y-6 py-6 2xl:grid-cols-[minmax(0,1fr)_360px]">
              <div className="min-w-0">
                <div className="mb-3 flex items-center gap-2">
                  <Status value={e.status} />
                  {e.error_code && <span className="font-mono text-xs text-seal">{e.error_code}</span>}
                </div>
                {e.answer && (
                  <AnswerText
                    answer={e.answer}
                    citations={e.citations}
                    tools={[]}
                    onFocus={ev.setFocus}
                    onOpenCitation={ev.openCitation}
                    onOpenTool={ev.openTool}
                  />
                )}
                <button
                  onClick={() => navigate("chat", e.id)}
                  className="mt-4 text-sm font-semibold text-stamp underline decoration-stamp/40 hover:decoration-stamp"
                >
                  Open as a case on Ask
                </button>
              </div>
              {e.answer && (
                <Exhibits
                  citations={e.citations}
                  tools={[]}
                  focus={ev.focus}
                  onFocus={ev.setFocus}
                  onOpenCitation={ev.openCitation}
                  onOpenTool={ev.openTool}
                  note={citesTools ? "Tool results ([T#]) are not stored with the execution; they are in the ledger below." : undefined}
                />
              )}
            </div>
          )}
          {ev.sheets}
          <h2 className="mb-1 mt-2 border-b-1.5 border-ink pb-2 text-lg font-bold">
            Ledger <span className="font-mono text-sm font-normal text-ink-2">{trace.data?.length ?? ""} events</span>
          </h2>
          {trace.loading && !trace.data ? <Spinner /> : trace.data && <Ledger events={trace.data} />}
        </article>
      )}
    </div>
  );
}

export default function Executions({ selected }: { selected?: string }) {
  const list = useLoad(() => api.executions(100));
  return (
    <div className="grid min-h-full xl:grid-cols-[320px_minmax(0,1fr)]">
      <aside className="border-r-1.5 border-ink bg-ground xl:sticky xl:top-0 xl:h-screen xl:overflow-y-auto">
        <div className="flex items-end justify-between gap-3 border-b-1.5 border-ink px-6 pb-3 pt-8">
          <div>
            <h1 className="text-2xl font-extrabold">Executions</h1>
            <p className="text-sm text-ink-2">Your runs, newest first</p>
          </div>
          <Button size="sm" variant="ghost" onClick={() => void list.reload()} busy={list.loading && !!list.data}>
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            <span className="sr-only">Refresh</span>
          </Button>
        </div>
        <div className="px-6 pt-3">
          <ErrorBox error={list.error} />
        </div>
        {list.data && list.data.length === 0 && (
          <div className="p-6">
            <Empty title="No runs yet">Ask something on the Ask screen.</Empty>
          </div>
        )}
        <ol className="divide-y divide-rule">
          {list.data?.map((e) => (
            <li key={e.id}>
              <button
                onClick={() => navigate("executions", e.id)}
                aria-current={selected === e.id ? "true" : undefined}
                className={cx(
                  "block w-full px-6 py-3 text-left transition-colors",
                  selected === e.id ? "bg-ink text-paper" : "hover:bg-wash",
                )}
              >
                <div className="flex items-center gap-2">
                  <span className={cx("font-mono text-xs font-bold", selected === e.id ? "text-paper" : "text-ink")}>{`EX-${e.id.replace(/-/g, "").slice(0, 4).toUpperCase()}`}</span>
                  <span className={cx("font-mono text-2xs", selected === e.id ? "text-paper/70" : "text-ink-2")}>{e.mode}</span>
                  <span className={cx("ml-auto font-mono text-xs", selected === e.id ? "text-paper/80" : "text-ink-2")}>{ms(e.total_ms)}</span>
                </div>
                <div className="mt-1 line-clamp-2 text-sm">{e.question}</div>
                <div className="mt-1.5 flex items-center gap-2">
                  {selected === e.id ? <span className="text-xs font-semibold">{label(e.status)}</span> : <Status value={e.status} />}
                  <span className={cx("ml-auto font-mono text-2xs", selected === e.id ? "text-paper/70" : "text-ink-3")}>{when(e.created_at)}</span>
                </div>
              </button>
            </li>
          ))}
        </ol>
      </aside>
      <div className="px-8 py-8">
        {selected ? (
          <Detail id={selected} />
        ) : (
          <div className="mx-auto mt-16 max-w-md">
            <Empty title="Select a run">Its custody chain, measured figures, and full ledger open here.</Empty>
          </div>
        )}
      </div>
    </div>
  );
}
