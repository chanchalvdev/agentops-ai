import { ArrowUpRight, CircleStop, CornerDownLeft, Stamp } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { CustodyChain } from "../components/CustodyChain";
import { AnswerText, Exhibits, useEvidence } from "../components/Evidence";
import { Button, ErrorBox, Figure, RecordId, Spinner, Status, cx, recordId } from "../components/ui";
import { api, streamAgent } from "../lib/api";
import { ms } from "../lib/format";
import { navigate } from "../lib/nav";
import type { AgentRunResult, LedgerEvent, StreamEvent } from "../lib/types";

type Mode = "graph" | "loop";

interface Case {
  question: string;
  mode: Mode;
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
  "Create a new file notes/ui-demo.md in my workspace containing the line: UI demo",
];

/** A stored execution read back as a case record (deep link #/chat/<execution id>). */
async function loadCase(id: string): Promise<Case> {
  const [e, events] = await Promise.all([api.execution(id), api.trace(id)]);
  const final = events.find((x) => x.kind === "final");
  const verification = final?.data?.verification;
  return {
    question: e.question,
    mode: e.mode === "loop" ? "loop" : "graph",
    executionId: e.id,
    events,
    running: false,
    result: {
      execution_id: e.id,
      mode: e.mode === "loop" ? "loop" : "graph",
      status: e.status,
      answer: e.answer ?? "",
      citations: e.citations,
      tool_references: [],
      error_code: e.error_code,
      verification: typeof verification === "string" ? verification : null,
      steps: e.steps,
      model_calls: e.model_calls,
      tool_calls: e.tool_calls,
      corrections: {},
      usage: { input_tokens: e.input_tokens, output_tokens: e.output_tokens },
      cost_usd: e.cost_usd,
      total_ms: e.total_ms ?? 0,
    },
  };
}

function CaseRecord({ c }: { c: Case }) {
  const ev = useEvidence();
  const r = c.result;
  return (
    <article className="animate-sign">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-2 border-b-1.5 border-ink px-4 sm:px-8 pb-4 pt-7">
        {c.executionId ? <RecordId prefix="EX" id={c.executionId} invert /> : <Spinner />}
        <h1 className="min-w-0 flex-1 text-2xl font-bold">{c.question}</h1>
        <span className="font-mono text-xs text-ink-2">{c.mode}</span>
      </header>
      <CustodyChain
        events={c.events}
        live={c.running}
        final={r ? { status: r.status, verification: r.verification, totalMs: r.total_ms } : null}
      />
      <div className="grid gap-x-10 gap-y-8 px-4 sm:px-8 py-7 xl:grid-cols-[minmax(0,1fr)_380px]">
        <section aria-label="Answer" className="min-w-0">
          {r ? (
            <>
              <div className="mb-4 flex items-center gap-2">
                <Status value={r.status} />
                {r.verification && (
                  <Status value={r.verification === "supported" ? "verified" : r.verification} tone={r.verification === "supported" ? "verified" : "pending"} />
                )}
              </div>
              <AnswerText
                answer={r.answer}
                citations={r.citations}
                tools={r.tool_references}
                onFocus={ev.setFocus}
                onOpenCitation={ev.openCitation}
                onOpenTool={ev.openTool}
              />
              {r.status === "awaiting_approval" && (
                <div className="mt-6 flex flex-wrap items-center gap-4 border-1.5 border-caution bg-caution-soft px-4 py-3">
                  <Stamp className="h-5 w-5 text-caution" aria-hidden />
                  <p className="flex-1 text-sm">
                    <span className="font-semibold">Held for your countersignature.</span> The agent proposed a change and
                    nothing has been written. Review the diff before it continues.
                  </p>
                  <Button variant="seal" onClick={() => navigate("approvals")}>
                    Review in Approvals
                  </Button>
                </div>
              )}
              <dl className="mt-8 grid grid-cols-2 gap-6 border-t border-rule pt-5 sm:grid-cols-5">
                <Figure value={ms(r.total_ms)} label="sealed in" />
                <Figure value={r.model_calls} label="model calls" />
                <Figure value={r.tool_calls} label="tool calls" />
                <Figure value={(r.usage.input_tokens + r.usage.output_tokens).toLocaleString()} label="tokens" />
                <Figure value={r.cost_usd == null ? "n/a" : `$${Number(r.cost_usd).toFixed(2)}`} label="cost" />
              </dl>
              <button
                onClick={() => navigate("executions", r.execution_id)}
                className="mt-5 inline-flex items-center gap-1 text-sm font-semibold text-stamp underline decoration-stamp/40 hover:decoration-stamp"
              >
                Full ledger for {recordId("EX", r.execution_id)} <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
              </button>
            </>
          ) : c.running ? (
            <p className="text-lg text-ink-2">
              The record is being signed. This usually takes 15–90 s; every step appears above as it completes.
            </p>
          ) : null}
          <div className="mt-4 space-y-3">
            {c.streamError && <ErrorBox error={new Error(`The run ended with ${c.streamError.replace(/_/g, " ")}.`)} />}
            <ErrorBox error={c.error} />
          </div>
        </section>
        {r && (
          <aside aria-label="Exhibits" className="min-w-0">
            <Exhibits
              citations={r.citations}
              tools={r.tool_references}
              focus={ev.focus}
              onFocus={ev.setFocus}
              onOpenCitation={ev.openCitation}
              onOpenTool={ev.openTool}
            />
          </aside>
        )}
      </div>
      {ev.sheets}
    </article>
  );
}

export default function Chat({ openId }: { openId?: string }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<Mode>("graph");
  const [cases, setCases] = useState<Case[]>([]);
  const [current, setCurrent] = useState(0);
  const [openError, setOpenError] = useState<unknown>(null);
  const abort = useRef<AbortController | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const running = cases.some((c) => c.running);

  useEffect(() => {
    if (!openId) return;
    let alive = true;
    loadCase(openId)
      .then((c) => {
        if (!alive) return;
        setCases([c]);
        setCurrent(0);
      })
      .catch((e) => alive && setOpenError(e));
    return () => {
      alive = false;
    };
  }, [openId]);

  const update = (idx: number, patch: (c: Case) => Case) =>
    setCases((all) => all.map((c, i) => (i === idx ? patch(c) : c)));

  async function ask(q: string) {
    if (!q.trim() || running) return;
    const idx = cases.length;
    setCases((all) => [...all, { question: q.trim(), mode, events: [], running: true }]);
    setCurrent(idx);
    setQuestion("");
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      await streamAgent(
        q.trim(),
        mode,
        (e: StreamEvent) => {
          if (e.event === "start") update(idx, (c) => ({ ...c, executionId: (e.data as { execution_id: string }).execution_id }));
          else if (e.event === "final") {
            const result = e.data as AgentRunResult;
            update(idx, (c) => ({ ...c, result }));
            if (result.status === "awaiting_approval") window.dispatchEvent(new Event("agentops:approvals"));
          } else if (e.event === "error") update(idx, (c) => ({ ...c, streamError: (e.data as { code: string }).code }));
          else if (e.event !== "citation") update(idx, (c) => ({ ...c, events: [...c.events, e.data as LedgerEvent] }));
        },
        ctrl.signal,
      );
    } catch (err) {
      if (!ctrl.signal.aborted) update(idx, (c) => ({ ...c, error: err }));
    } finally {
      update(idx, (c) => ({ ...c, running: false }));
      window.dispatchEvent(new Event("agentops:counts"));
    }
  }

  const shown = cases[current];
  const earlier = cases.map((c, i) => ({ c, i })).filter(({ i }) => i !== current).reverse();

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex-1">
        {openError ? (
          <div className="px-4 sm:px-8 pt-8">
            <ErrorBox error={openError} />
          </div>
        ) : null}
        {shown ? (
          <CaseRecord key={current} c={shown} />
        ) : openId && !openError ? (
          <div className="px-4 sm:px-8 pt-10">
            <Spinner />
          </div>
        ) : (
          <div className="mx-auto max-w-3xl px-4 sm:px-8 pb-10 pt-16">
            <h1 className="text-3xl font-extrabold">Ask your documents.</h1>
            <p className="mt-2 max-w-xl text-lg text-ink-2">
              Every answer comes back as a record: each step signed by the model or tool that took it, sealed with its
              verification, and every claim tied to the passage it came from.
            </p>
            <h2 className="mt-10 border-b-1.5 border-ink pb-2 text-md font-bold">Try a question from the sample corpus</h2>
            <ul className="divide-y divide-rule">
              {EXAMPLES.map((q) => (
                <li key={q}>
                  <button
                    onClick={() => {
                      setQuestion(q);
                      input.current?.focus();
                    }}
                    className="group flex w-full items-center gap-3 py-3 text-left text-md hover:text-stamp"
                  >
                    <span className="flex-1">{q}</span>
                    <CornerDownLeft className="h-4 w-4 text-ink-3 group-hover:text-stamp" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        {earlier.length > 0 && (
          <section className="px-4 sm:px-8 pb-8" aria-label="Earlier cases this session">
            <h2 className="border-b-1.5 border-ink pb-2 text-md font-bold">Earlier this session</h2>
            <ul className="divide-y divide-rule">
              {earlier.map(({ c, i }) => (
                <li key={i}>
                  <button onClick={() => setCurrent(i)} className="flex w-full items-center gap-4 py-2.5 text-left hover:bg-wash">
                    {c.executionId ? <RecordId prefix="EX" id={c.executionId} /> : <span className="w-[62px]" />}
                    <span className="min-w-0 flex-1 truncate text-sm">{c.question}</span>
                    {c.result ? <Status value={c.result.status} /> : c.running ? <Spinner /> : <Status value="error" />}
                    <span className="w-16 text-right font-mono text-xs text-ink-2">{c.result ? ms(c.result.total_ms) : ""}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      <form
        className="sticky bottom-0 z-10 border-t-1.5 border-ink bg-ground px-4 sm:px-8 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
      >
        <div className="flex items-end gap-3">
          <label htmlFor="question" className="sr-only">
            Question
          </label>
          <textarea
            id="question"
            ref={input}
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
            className="min-h-[60px] flex-1 resize-none border-1.5 border-ink/40 bg-paper px-3.5 py-2.5 text-md text-ink hover:border-ink/70 focus:border-stamp focus:outline-none"
          />
          <div className="flex flex-col gap-2">
            <div role="radiogroup" aria-label="Agent mode" className="flex border-1.5 border-ink">
              {(["graph", "loop"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={mode === m}
                  onClick={() => setMode(m)}
                  title={m === "graph" ? "LangGraph workflow: verification and approvals" : "The hand-written agent loop"}
                  className={cx(
                    "h-7 flex-1 px-3 text-xs font-semibold capitalize transition-colors",
                    mode === m ? "bg-ink text-paper" : "bg-paper text-ink hover:bg-wash",
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
            {running ? (
              <Button variant="danger" onClick={() => abort.current?.abort()}>
                <CircleStop className="h-4 w-4" aria-hidden /> Stop
              </Button>
            ) : (
              <Button type="submit" variant="primary" disabled={!question.trim()}>
                Ask <CornerDownLeft className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )}
          </div>
        </div>
      </form>
    </div>
  );
}
