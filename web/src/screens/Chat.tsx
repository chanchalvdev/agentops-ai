import { useRef, useState } from "react";
import { Badge, Button, Card, Drawer, ErrorBox, Mono, Spinner, cx } from "../components/ui";
import { api, streamAgent } from "../lib/api";
import { ms, splitCitations } from "../lib/format";
import { navigate } from "../lib/nav";
import type { AgentRunResult, ChunkOut, Citation, LedgerEvent, StreamEvent, ToolReference } from "../lib/types";

interface Turn {
  question: string;
  mode: "graph" | "loop";
  executionId?: string;
  events: LedgerEvent[];
  result?: AgentRunResult;
  error?: unknown;
  streamError?: string;
  running: boolean;
}

const EXAMPLES = [
  "What is the RPO in the latest payments architecture?",
  "What is the RPO in v2, in seconds?",
  "Compare the availability targets of payments architecture v1 and v2.",
  "Add a function minutes_to_seconds to calc/mathlib.py in my workspace",
];

function eventLabel(e: LedgerEvent): string {
  const d = e.data ?? {};
  if (e.kind === "tool_call") {
    const cache = d.cache === "hit" ? " (cache hit)" : "";
    return `${e.name}${cache}`;
  }
  if (e.kind === "model_call") return `${e.name} · ${String(d.model ?? "")}`;
  return e.name;
}

function Timeline({ events, running }: { events: LedgerEvent[]; running: boolean }) {
  return (
    <ol className="space-y-1 text-xs">
      {events.map((e) => (
        <li key={e.seq} className="flex items-center gap-2">
          <span className="w-6 text-right tabular-nums text-slate-400">{e.seq}</span>
          <Badge tone={e.status === "ok" ? "gray" : e.status === "pending" ? "amber" : "red"}>{e.kind}</Badge>
          <span className={cx("font-mono", e.status === "rejected" || e.status === "error" ? "text-rose-600" : "")}>
            {eventLabel(e)}
          </span>
          {e.error_code && <span className="text-rose-600">{e.error_code}</span>}
          {e.message && e.status !== "ok" && <span className="truncate text-slate-500">{e.message}</span>}
          <span className="ml-auto tabular-nums text-slate-500">
            {e.latency_ms != null ? ms(e.latency_ms) : ""}
            {e.input_tokens ? ` · ${e.input_tokens}+${e.output_tokens ?? 0} tok` : ""}
          </span>
        </li>
      ))}
      {running && (
        <li className="flex items-center gap-2 pl-8 text-slate-500">
          <Spinner /> working…
        </li>
      )}
    </ol>
  );
}

function Answer({
  result,
  onCitation,
  onTool,
}: {
  result: AgentRunResult;
  onCitation: (c: Citation) => void;
  onTool: (t: ToolReference) => void;
}) {
  const citations = new Map(result.citations.map((c) => [c.source_id, c]));
  const tools = new Map(result.tool_references.map((t) => [t.source_id, t]));
  return (
    <div className="space-y-3">
      <p className="whitespace-pre-wrap leading-relaxed">
        {splitCitations(result.answer).map((seg, i) => {
          if (typeof seg === "string") return <span key={i}>{seg}</span>;
          const c = citations.get(seg.label);
          const t = tools.get(seg.label);
          if (!c && !t) return <span key={i}>[{seg.label}]</span>;
          return (
            <button
              key={i}
              onClick={() => (c ? onCitation(c) : onTool(t!))}
              className="mx-0.5 rounded bg-indigo-100 px-1 font-mono text-xs text-indigo-700 hover:bg-indigo-200 dark:bg-indigo-900/50 dark:text-indigo-300"
              title={c ? `${c.filename} §${c.section ?? "-"}` : t!.tool}
            >
              {seg.label}
            </button>
          );
        })}
      </p>
      {(result.citations.length > 0 || result.tool_references.length > 0) && (
        <ul className="space-y-1 text-xs">
          {result.citations.map((c) => (
            <li key={c.source_id}>
              <button className="text-left hover:underline" onClick={() => onCitation(c)}>
                <span className="font-mono text-indigo-600 dark:text-indigo-400">{c.source_id}</span> {c.filename}
                {c.section ? ` § ${c.section}` : ""}
                {c.page_start ? ` p${c.page_start}` : ""}
              </button>
            </li>
          ))}
          {result.tool_references.map((t) => (
            <li key={t.source_id}>
              <button className="text-left hover:underline" onClick={() => onTool(t)}>
                <span className="font-mono text-indigo-600 dark:text-indigo-400">{t.source_id}</span> {t.tool} result
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <Badge>{result.status}</Badge>
        {result.verification && <Badge tone={result.verification === "supported" ? "green" : "amber"}>verify: {result.verification}</Badge>}
        <span>
          {result.model_calls} model calls · {result.tool_calls} tool calls · {result.usage.input_tokens + result.usage.output_tokens} tokens ·{" "}
          {ms(result.total_ms)}
        </span>
        <button className="underline" onClick={() => navigate("executions", result.execution_id)}>
          trace
        </button>
      </div>
      {result.status === "awaiting_approval" && (
        <div className="flex items-center gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-900 dark:bg-amber-950/40">
          This run is paused for a human decision.
          <Button variant="primary" onClick={() => navigate("approvals")}>
            Review in Approvals
          </Button>
        </div>
      )}
    </div>
  );
}

export default function Chat() {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<"graph" | "loop">("graph");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [chunk, setChunk] = useState<{ citation: Citation; data?: ChunkOut; error?: unknown } | null>(null);
  const [tool, setTool] = useState<ToolReference | null>(null);
  const abort = useRef<AbortController | null>(null);
  const running = turns.some((t) => t.running);

  const update = (idx: number, patch: (t: Turn) => Turn) =>
    setTurns((all) => all.map((t, i) => (i === idx ? patch(t) : t)));

  async function ask(q: string) {
    if (!q.trim() || running) return;
    const idx = turns.length;
    setTurns((all) => [...all, { question: q, mode, events: [], running: true }]);
    setQuestion("");
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      await streamAgent(
        q,
        mode,
        (e: StreamEvent) => {
          if (e.event === "start") update(idx, (t) => ({ ...t, executionId: (e.data as { execution_id: string }).execution_id }));
          else if (e.event === "final") update(idx, (t) => ({ ...t, result: e.data as AgentRunResult }));
          else if (e.event === "error") update(idx, (t) => ({ ...t, streamError: (e.data as { code: string }).code }));
          else if (e.event !== "citation") update(idx, (t) => ({ ...t, events: [...t.events, e.data as LedgerEvent] }));
        },
        ctrl.signal,
      );
    } catch (err) {
      if (!ctrl.signal.aborted) update(idx, (t) => ({ ...t, error: err }));
    } finally {
      update(idx, (t) => ({ ...t, running: false }));
    }
  }

  async function openCitation(c: Citation) {
    setChunk({ citation: c });
    try {
      setChunk({ citation: c, data: await api.chunk(c.document_id, c.chunk_id) });
    } catch (error) {
      setChunk({ citation: c, error });
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold">Chat</h1>
      {turns.length === 0 && (
        <Card title="Try one of these">
          <div className="flex flex-wrap gap-2">
            {EXAMPLES.map((q) => (
              <Button key={q} onClick={() => setQuestion(q)}>
                {q}
              </Button>
            ))}
          </div>
        </Card>
      )}
      {turns.map((t, i) => (
        <Card
          key={i}
          title={<span className="font-normal">{t.question}</span>}
          actions={<Badge tone="gray">{t.mode}</Badge>}
        >
          <div className="grid gap-4 md:grid-cols-[1fr_minmax(0,1.1fr)]">
            <div>
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Steps</div>
              <Timeline events={t.events} running={t.running} />
            </div>
            <div>
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Answer</div>
              {t.result ? (
                <Answer result={t.result} onCitation={openCitation} onTool={setTool} />
              ) : t.running ? (
                <p className="text-sm text-slate-500">Streaming…</p>
              ) : null}
              {t.streamError && <ErrorBox error={new Error(`Run ended with ${t.streamError}`)} />}
              <ErrorBox error={t.error} />
            </div>
          </div>
        </Card>
      ))}
      <form
        className="sticky bottom-0 flex gap-2 rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-900"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
      >
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void ask(question);
            }
          }}
          rows={2}
          maxLength={2000}
          placeholder="Ask about your documents, or ask the agent to change a file in your workspace…"
          className="min-w-0 flex-1 resize-none rounded-md border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
        />
        <div className="flex flex-col gap-2">
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "graph" | "loop")}
            className="rounded-md border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
            title="graph: LangGraph workflow with verification and approvals. loop: the hand-written agent loop."
          >
            <option value="graph">graph</option>
            <option value="loop">loop</option>
          </select>
          {running ? (
            <Button variant="danger" onClick={() => abort.current?.abort()}>
              Stop
            </Button>
          ) : (
            <Button type="submit" variant="primary" disabled={!question.trim()}>
              Ask
            </Button>
          )}
        </div>
      </form>

      <Drawer open={!!chunk} onClose={() => setChunk(null)} title={chunk ? `${chunk.citation.source_id} · ${chunk.citation.filename}` : ""}>
        {chunk && (
          <div className="space-y-3 text-sm">
            <div className="text-xs text-slate-500">
              {chunk.citation.section ? `§ ${chunk.citation.section}` : ""}
              {chunk.data ? ` · chunk ${chunk.data.chunk_index} · version ${chunk.data.document_version}` : ""}
            </div>
            <ErrorBox error={chunk.error} />
            {chunk.data ? <Mono>{chunk.data.text}</Mono> : !chunk.error && <Spinner />}
          </div>
        )}
      </Drawer>
      <Drawer open={!!tool} onClose={() => setTool(null)} title={tool ? `${tool.source_id} · ${tool.tool}` : ""}>
        {tool && <Mono>{JSON.stringify(tool.output, null, 2)}</Mono>}
      </Drawer>
    </div>
  );
}
