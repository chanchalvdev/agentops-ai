import { GitCompare, Play, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Button, Empty, ErrorBox, Figure, PageHead, Panel, RecordId, Spinner, Status, cx, label, selectClass, useLoad } from "../components/ui";
import { ApiError, api } from "../lib/api";
import { ms, num, pct, when } from "../lib/format";
import { navigate } from "../lib/nav";
import type { CheckOut, Comparison, Signal } from "../lib/types";

function RateBar({ rate }: { rate: number | null }) {
  const v = rate ?? 0;
  return (
    <div className="flex items-center gap-3">
      <div className="h-1.5 w-full min-w-16 bg-wash" aria-hidden>
        <div className={cx("h-1.5", v >= 0.9 ? "bg-record" : v >= 0.6 ? "bg-caution" : "bg-seal")} style={{ width: `${v * 100}%` }} />
      </div>
      <span className="w-12 text-right font-mono text-xs">{pct(rate)}</span>
    </div>
  );
}

function ScoreTable({ title, rows }: { title: string; rows: [string, { passed: number; total: number; rate: number | null }][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-sm">
        <thead>
          <tr className="border-b border-rule text-left text-xs text-ink-2">
            <th className="py-2 pr-3 font-semibold">{title}</th>
            <th className="px-3 text-right font-semibold">Passed</th>
            <th className="w-[45%] pl-3 font-semibold">Rate</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule/70">
          {rows.map(([name, v]) => (
            <tr key={name}>
              <td className="py-2 pr-3 font-mono text-xs">{name}</td>
              <td className="px-3 text-right font-mono text-xs">
                {v.passed}/{v.total}
              </td>
              <td className="pl-3">
                <RateBar rate={v.rate} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunDetail({ id }: { id: string }) {
  const run = useLoad(() => api.evalRun(id), [id]);
  const r = run.data;
  if (run.error) return <ErrorBox error={run.error} />;
  if (!r) return <Spinner />;
  const a = r.aggregate;
  const failed = r.cases.filter((c) => c.passed === false);
  return (
    <Panel
      title={
        <span className="flex items-center gap-3">
          <RecordId prefix="EV" id={r.run_id} invert />
          {String(r.config.profile ?? "default configuration")}
        </span>
      }
      meta={`${r.mode} · prompt ${String(r.config.prompt_version ?? "–")}`}
    >
      <dl className="grid grid-cols-2 gap-6 border-b border-rule pb-6 sm:grid-cols-5">
        <Figure value={`${a.passed}/${a.total}`} label={`passed · ${pct(a.pass_rate)}`} />
        <Figure value={ms(a.latency_ms.p50)} label="median latency" />
        <Figure value={ms(a.latency_ms.p95)} label="p95 latency" />
        <Figure value={num(a.tokens_per_request ?? null)} label="tokens / request" />
        <Figure value={a.quality_score == null ? "–" : a.quality_score.toFixed(3)} label="judge score (advisory)" />
      </dl>
      <div className="grid gap-8 py-6 lg:grid-cols-2">
        <ScoreTable title="Category" rows={Object.entries(a.by_category).map(([k, v]) => [k, { passed: v.passed, total: v.total, rate: v.pass_rate }])} />
        <ScoreTable title="Evaluator" rows={Object.entries(a.by_evaluator).map(([k, v]) => [k, { passed: v.passed, total: v.applicable, rate: v.pass_rate }])} />
      </div>
      <h3 className="border-b-1.5 border-ink pb-2 text-md font-bold">
        Failing cases <span className="font-mono text-sm font-normal text-ink-2">{failed.length}</span>
      </h3>
      {failed.length === 0 ? (
        <p className="py-4 text-sm text-ink-2">Every case passed.</p>
      ) : (
        <ul className="divide-y divide-rule">
          {failed.map((c) => (
            <li key={c.id} className="py-3">
              <div className="flex flex-wrap items-center gap-3">
                <span className="font-mono text-sm font-bold">{c.id}</span>
                <Status value={c.status} />
                <span className="text-xs text-ink-2">{c.category}</span>
                {c.execution_id && (
                  <button
                    className="ml-auto text-xs font-semibold text-stamp underline decoration-stamp/40 hover:decoration-stamp"
                    onClick={() => navigate("executions", c.execution_id!)}
                  >
                    Ledger
                  </button>
                )}
              </div>
              <ul className="mt-1.5 space-y-0.5">
                {c.failures.map((f) => (
                  <li key={f} className="text-sm text-ink-2">
                    {f}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function CompareRuns({ runs }: { runs: { id: string; label: string }[] }) {
  const [base, setBase] = useState("");
  const [head, setHead] = useState("");
  const [result, setResult] = useState<Comparison | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const select = (value: string, set: (v: string) => void, name: string) => (
    <label className="min-w-0 flex-1">
      <span className="mb-1.5 block text-xs font-semibold text-ink-2">{name}</span>
      <select value={value} onChange={(e) => set(e.target.value)} className={cx(selectClass, "w-full")}>
        <option value="">Choose a completed run</option>
        {runs.map((r) => (
          <option key={r.id} value={r.id}>
            {r.label}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <Panel title="Regression gate" meta="Compare a run against a baseline with the committed thresholds">
      <div className="flex flex-wrap items-end gap-3">
        {select(base, setBase, "Baseline")}
        {select(head, setHead, "Candidate")}
        <Button
          variant="primary"
          disabled={!base || !head}
          busy={busy}
          onClick={async () => {
            setError(null);
            setBusy(true);
            try {
              setResult(await api.compare(base, head));
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          <GitCompare className="h-4 w-4" aria-hidden /> Compare
        </Button>
      </div>
      <div className="mt-4 space-y-3">
        <ErrorBox error={error} />
        {result && (
          <div className="grid gap-6 border-1.5 border-ink bg-paper p-5 md:grid-cols-[auto_1fr]">
            <div
              className={cx(
                "flex -rotate-2 animate-stamp flex-col items-center justify-center self-start border-2 px-4 py-2 text-center",
                result.breaches.length ? "border-seal text-seal" : "border-record text-record",
              )}
            >
              <span className="text-sm font-extrabold uppercase tracking-[0.08em]">{result.breaches.length ? "Gate fails" : "Gate passes"}</span>
              <span className="font-mono text-2xs">
                {pct(result.pass_rate.base)} → {pct(result.pass_rate.head)}
              </span>
            </div>
            <div className="space-y-2 text-sm">
              {result.breaches.length ? (
                <ul className="space-y-1">
                  {result.breaches.map((b) => (
                    <li key={b} className="text-seal">
                      {b}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No threshold is breached: the candidate is at least as good as the baseline on every gated measure.</p>
              )}
              {result.newly_failing.length > 0 && (
                <p>
                  <span className="font-semibold">Newly failing:</span> <span className="font-mono text-xs">{result.newly_failing.join(", ")}</span>
                </p>
              )}
              {result.newly_passing.length > 0 && (
                <p>
                  <span className="font-semibold">Newly passing:</span> <span className="font-mono text-xs">{result.newly_passing.join(", ")}</span>
                </p>
              )}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

const FAMILY_ORDER = ["quality", "retrieval", "data", "operational"];

function Signals({ signals }: { signals: Signal[] }) {
  const order = { drift: 0, insufficient_data: 1, ok: 2 } as const;
  const byFamily = FAMILY_ORDER.map((f) => [f, signals.filter((s) => s.category === f).sort((a, b) => order[a.status] - order[b.status])] as const);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-sm">
        <thead>
          <tr className="border-b border-rule text-left text-xs text-ink-2">
            <th className="py-2 pr-3 font-semibold">Signal</th>
            <th className="px-3 font-semibold">Status</th>
            <th className="px-3 text-right font-semibold">Baseline</th>
            <th className="px-3 text-right font-semibold">Now</th>
            <th className="pl-3 font-semibold">What changed</th>
          </tr>
        </thead>
        {byFamily.map(([family, rows]) =>
          rows.length ? (
            <tbody key={family}>
              <tr>
                <th colSpan={5} scope="rowgroup" className="pb-1 pt-5 text-left text-md font-bold capitalize">
                  {family}
                </th>
              </tr>
              {rows.map((s) => (
                <tr key={`${s.category}.${s.metric}`} className={cx("border-t border-rule/70 align-top", s.status === "drift" && "bg-seal-soft/60")}>
                  <td className="py-2 pr-3 font-mono text-xs">{s.metric}</td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <Status
                      value={s.status === "drift" ? `${s.evidence ?? ""} drift`.trim() : s.status}
                      tone={s.status === "drift" ? (s.evidence === "measured" ? "critical" : "pending") : undefined}
                    />
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">{s.baseline ?? "–"}</td>
                  <td className="px-3 py-2 text-right font-mono text-xs">{s.current ?? "–"}</td>
                  <td className={cx("max-w-[460px] py-2 pl-3 text-xs", s.status === "drift" ? "text-ink" : "text-ink-2")}>
                    <span className="line-clamp-3 break-words" title={s.message}>
                      {s.message}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          ) : null,
        )}
      </table>
    </div>
  );
}

function Monitoring() {
  const summary = useLoad(() => api.monitoringSummary());
  const drift = useLoad(() => api.latestDrift());
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const check: CheckOut | null = drift.data;
  const s = summary.data;
  const op = s?.operational;
  const status = s?.drift_status ?? null;
  return (
    <div className="space-y-10">
      <section className="flex flex-wrap items-center gap-6 border-1.5 border-ink bg-paper px-6 py-5">
        <div
          className={cx(
            "flex -rotate-2 flex-col items-center border-2 px-4 py-2",
            status === "drift" ? "border-seal text-seal" : status === "ok" ? "border-record text-record" : "border-caution text-caution",
          )}
        >
          <span className="text-sm font-extrabold uppercase tracking-[0.08em]">{status ? label(status) : "No check yet"}</span>
          <span className="font-mono text-2xs">{s?.last_check_at ? when(s.last_check_at) : "run one"}</span>
        </div>
        <dl className="grid min-w-0 flex-1 grid-cols-2 gap-6 sm:grid-cols-5">
          <Figure value={num(op?.executions)} label="runs, 24 h" />
          <Figure value={pct(op?.success_rate)} label="success rate" />
          <Figure value={ms(op?.latency_ms.p95)} label="p95 latency" />
          <Figure value={pct(op?.provider_error_rate, 1)} label="provider errors" />
          <Figure value={num(op?.tokens.per_execution)} label="tokens / run" />
        </dl>
        <Button
          variant="primary"
          busy={checking}
          onClick={async () => {
            setChecking(true);
            setError(null);
            try {
              drift.setData(await api.runDriftCheck());
              await summary.reload();
            } catch (e) {
              setError(e);
            } finally {
              setChecking(false);
            }
          }}
        >
          <Play className="h-4 w-4" aria-hidden /> Run check now
        </Button>
      </section>
      <ErrorBox error={error ?? drift.error ?? summary.error} />

      <Panel title="Signals" meta={check ? `Check of ${when(check.created_at)} against the committed baselines` : undefined} flush>
        {check ? (
          <Signals signals={check.report.signals} />
        ) : drift.loading ? (
          <div className="py-6">
            <Spinner />
          </div>
        ) : (
          <div className="pt-4">
            <Empty title="No drift check has run">Run one to compare quality, retrieval, data, and operations with the baselines.</Empty>
          </div>
        )}
      </Panel>

      <Panel title="Alerts" meta={s ? `${s.open_alerts.length} open` : undefined}>
        {s && s.open_alerts.length === 0 ? (
          <Empty title="No open alerts" />
        ) : (
          <ul className="divide-y divide-rule">
            {s?.open_alerts.map((a) => (
              <li key={a.id} className="flex gap-4 py-3">
                <ShieldAlert className={cx("mt-0.5 h-4 w-4 shrink-0", a.severity === "critical" ? "text-seal" : "text-caution")} aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-bold">
                      {a.category}.{a.metric}
                    </span>
                    <Status value={a.severity} />
                    <Status value={a.evidence} tone={a.evidence === "measured" ? "critical" : "pending"} />
                    <span className="ml-auto font-mono text-xs text-ink-2">{when(a.created_at)}</span>
                  </div>
                  <p className="mt-1 text-sm text-ink-2">{a.message}</p>
                  <div className="mt-2">
                  <Button
                    size="sm"
                    title="Mark as seen; it stays on record"
                    onClick={async () => {
                      try {
                        await api.acknowledgeAlert(a.id);
                        await summary.reload();
                      } catch (e) {
                        setError(e);
                      }
                    }}
                  >
                    Acknowledge
                  </Button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

export default function Evaluations({ selected }: { selected?: string }) {
  const runs = useLoad(() => api.evalRuns());
  const [tab, setTab] = useState<"runs" | "monitoring">("runs");
  const forbidden = runs.error instanceof ApiError && runs.error.status === 403;
  const completed = (runs.data ?? []).filter((r) => r.status === "completed");
  const options = completed.map((r) => ({
    id: r.id,
    label: `${String(r.config.profile ?? r.mode)} · ${r.passed_cases}/${r.total_cases} · ${when(r.started_at)}`,
  }));
  return (
    <div className="mx-auto max-w-[1400px] px-4 sm:px-8 py-6 sm:py-8">
      <PageHead title="Evals & drift" sub="Measured quality: the golden set, the regression gate, and drift against committed baselines." />
      {forbidden ? (
        <div className="max-w-2xl border-1.5 border-ink bg-paper p-6">
          <h2 className="text-lg font-bold">Operators only</h2>
          <p className="mt-2 text-md text-ink-2">
            Evaluations and monitoring run the shared model for minutes and read data across tenants, so only operator
            tenants may use them. Add this tenant to <code className="font-mono text-sm">AGENTOPS_ADMIN_TENANTS</code> on the
            server; in development, <code className="font-mono text-sm">tenant_a</code> is one.
          </p>
        </div>
      ) : (
        <>
          <div role="tablist" aria-label="Evaluations and monitoring" className="mb-8 flex border-b-1.5 border-ink">
            {(
              [
                ["runs", "Evaluation runs"],
                ["monitoring", "Drift monitoring"],
              ] as const
            ).map(([id, name]) => (
              <button
                key={id}
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={cx(
                  "-mb-[1.5px] h-10 border-1.5 px-4 text-sm font-semibold transition-colors",
                  tab === id ? "border-ink border-b-ground bg-ground text-ink" : "border-transparent text-ink-2 hover:text-ink",
                )}
              >
                {name}
              </button>
            ))}
          </div>
          {tab === "monitoring" ? (
            <Monitoring />
          ) : (
            <div className="grid gap-10 xl:grid-cols-[340px_minmax(0,1fr)]">
              <aside className="min-w-0">
                <h2 className="border-b-1.5 border-ink pb-2 text-lg font-bold">
                  Runs <span className="font-mono text-sm font-normal text-ink-2">{runs.data?.length ?? ""}</span>
                </h2>
                <ErrorBox error={runs.error} />
                {!runs.data && runs.loading && (
                  <div className="py-4">
                    <Spinner />
                  </div>
                )}
                {runs.data && runs.data.length === 0 && (
                  <div className="pt-4">
                    <Empty title="No runs">
                      Start one with <code className="font-mono">agentops eval run --judge</code>.
                    </Empty>
                  </div>
                )}
                <ol className="divide-y divide-rule">
                  {runs.data?.map((r) => (
                    <li key={r.id}>
                      <button
                        onClick={() => navigate("evaluations", r.id)}
                        aria-current={selected === r.id ? "true" : undefined}
                        className={cx("block w-full px-3 py-2.5 text-left transition-colors", selected === r.id ? "bg-ink text-paper" : "hover:bg-wash")}
                      >
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs font-bold">{`EV-${r.id.replace(/-/g, "").slice(0, 4).toUpperCase()}`}</span>
                          <span className="truncate text-sm font-semibold">{String(r.config.profile ?? "default")}</span>
                          <span className="ml-auto font-mono text-sm">
                            {r.passed_cases}/{r.total_cases}
                          </span>
                        </div>
                        <div className="mt-1 flex items-center gap-2">
                          {selected === r.id ? <span className="text-xs font-semibold">{label(r.status)}</span> : <Status value={r.status} />}
                          <span className={cx("text-xs", selected === r.id ? "text-paper/70" : "text-ink-2")}>{r.mode}</span>
                          <span className={cx("ml-auto font-mono text-2xs", selected === r.id ? "text-paper/70" : "text-ink-3")}>{when(r.started_at)}</span>
                        </div>
                      </button>
                    </li>
                  ))}
                </ol>
              </aside>
              <div className="min-w-0 space-y-12">
                <CompareRuns runs={options} />
                {selected ? (
                  <RunDetail id={selected} />
                ) : (
                  <Empty title="Select a run">Its scores by category and evaluator, and every failing case, open here.</Empty>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
