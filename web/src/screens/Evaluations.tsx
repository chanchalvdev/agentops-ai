import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Spinner, Stat, cx, useLoad } from "../components/ui";
import { ApiError, api } from "../lib/api";
import { ms, num, pct, when } from "../lib/format";
import { navigate } from "../lib/nav";
import type { CheckOut, Comparison, Signal } from "../lib/types";

function RunDetail({ id }: { id: string }) {
  const run = useLoad(() => api.evalRun(id), [id]);
  const r = run.data;
  if (run.error) return <ErrorBox error={run.error} />;
  if (!r) return <Spinner />;
  const a = r.aggregate;
  const failed = r.cases.filter((c) => c.passed === false);
  return (
    <Card title={`Run ${r.run_id.slice(0, 8)} · ${r.mode}${r.config.profile ? ` · ${String(r.config.profile)}` : ""}`}>
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
          <Stat label="Passed" value={`${a.passed}/${a.total}`} />
          <Stat label="Pass rate" value={pct(a.pass_rate)} />
          <Stat label="p50 latency" value={ms(a.latency_ms.p50)} />
          <Stat label="p95 latency" value={ms(a.latency_ms.p95)} />
          <Stat label="Tokens / request" value={num(a.tokens_per_request ?? null)} />
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <table className="text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr><th>Category</th><th className="text-right">Passed</th><th className="text-right">Rate</th></tr>
            </thead>
            <tbody>
              {Object.entries(a.by_category).map(([name, v]) => (
                <tr key={name} className="border-t border-slate-100 dark:border-slate-800">
                  <td className="py-1">{name}</td>
                  <td className="text-right tabular-nums">{v.passed}/{v.total}</td>
                  <td className="text-right tabular-nums">{pct(v.pass_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr><th>Evaluator</th><th className="text-right">Passed</th><th className="text-right">Rate</th></tr>
            </thead>
            <tbody>
              {Object.entries(a.by_evaluator).map(([name, v]) => (
                <tr key={name} className="border-t border-slate-100 dark:border-slate-800">
                  <td className="py-1 font-mono text-xs">{name}</td>
                  <td className="text-right tabular-nums">{v.passed}/{v.applicable}</td>
                  <td className="text-right tabular-nums">{pct(v.pass_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Failing cases</div>
          {failed.length === 0 ? (
            <p className="text-sm text-slate-500">None.</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {failed.map((c) => (
                <li key={c.id}>
                  <span className="font-mono">{c.id}</span> <Badge>{c.status}</Badge>
                  {c.execution_id && (
                    <button className="ml-2 text-xs underline" onClick={() => navigate("executions", c.execution_id!)}>
                      trace
                    </button>
                  )}
                  <div className="text-xs text-slate-500">{c.failures.join("; ")}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}

function CompareRuns({ runs }: { runs: { id: string; label: string }[] }) {
  const [base, setBase] = useState("");
  const [head, setHead] = useState("");
  const [result, setResult] = useState<Comparison | null>(null);
  const [error, setError] = useState<unknown>(null);
  const select = (value: string, set: (v: string) => void) => (
    <select
      value={value}
      onChange={(e) => set(e.target.value)}
      className="max-w-xs rounded-md border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
    >
      <option value="">choose a run…</option>
      {runs.map((r) => (
        <option key={r.id} value={r.id}>{r.label}</option>
      ))}
    </select>
  );
  return (
    <Card title="Compare with a baseline run">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        base {select(base, setBase)} head {select(head, setHead)}
        <Button
          variant="primary"
          disabled={!base || !head}
          onClick={async () => {
            setError(null);
            try {
              setResult(await api.compare(base, head));
            } catch (e) {
              setError(e);
            }
          }}
        >
          Compare
        </Button>
      </div>
      <div className="mt-3 space-y-2 text-sm">
        <ErrorBox error={error} />
        {result && (
          <>
            <p>
              pass rate {pct(result.pass_rate.base)} → {pct(result.pass_rate.head)}
            </p>
            {result.breaches.length ? (
              <ul className="list-disc pl-5 text-rose-700 dark:text-rose-400">
                {result.breaches.map((b) => <li key={b}>{b}</li>)}
              </ul>
            ) : (
              <p className="text-emerald-700 dark:text-emerald-400">No threshold breaches: the regression gate passes.</p>
            )}
            {result.newly_failing.length > 0 && <p>newly failing: <span className="font-mono">{result.newly_failing.join(", ")}</span></p>}
            {result.newly_passing.length > 0 && <p>newly passing: <span className="font-mono">{result.newly_passing.join(", ")}</span></p>}
          </>
        )}
      </div>
    </Card>
  );
}

function Signals({ signals }: { signals: Signal[] }) {
  const order = { drift: 0, insufficient_data: 1, ok: 2 } as const;
  const sorted = [...signals].sort((a, b) => order[a.status] - order[b.status]);
  return (
    <table className="w-full text-xs">
      <thead className="text-left text-slate-500">
        <tr><th>Family</th><th>Signal</th><th>Status</th><th className="text-right">Baseline</th><th className="text-right">Current</th><th>Detail</th></tr>
      </thead>
      <tbody>
        {sorted.map((s) => (
          <tr key={`${s.category}.${s.metric}`} className="border-t border-slate-100 align-top dark:border-slate-800">
            <td className="py-1">{s.category}</td>
            <td className="font-mono">{s.metric}</td>
            <td className="whitespace-nowrap">
              <Badge>{s.status}</Badge> {s.evidence && <Badge tone={s.evidence === "measured" ? "red" : "amber"}>{s.evidence}</Badge>}
            </td>
            <td className="text-right tabular-nums">{s.baseline ?? "–"}</td>
            <td className="text-right tabular-nums">{s.current ?? "–"}</td>
            <td className={cx("max-w-md", s.status === "drift" ? "" : "text-slate-500")}>{s.message}</td>
          </tr>
        ))}
      </tbody>
    </table>
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
  return (
    <div className="space-y-4">
      <Card
        title={
          <span className="flex items-center gap-2">
            Drift status {s?.drift_status ? <Badge>{s.drift_status}</Badge> : <Badge tone="gray">no check yet</Badge>}
          </span>
        }
        actions={
          <Button
            variant="primary"
            disabled={checking}
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
            {checking ? <Spinner /> : null} Run check now
          </Button>
        }
      >
        <div className="space-y-3">
          <ErrorBox error={error ?? drift.error} />
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            <Stat label="Executions (window)" value={num(op?.executions)} />
            <Stat label="Success rate" value={pct(op?.success_rate)} />
            <Stat label="p95 latency" value={ms(op?.latency_ms.p95)} />
            <Stat label="Provider error rate" value={pct(op?.provider_error_rate, 1)} />
            <Stat label="Tokens / execution" value={num(op?.tokens.per_execution)} />
          </div>
          {check ? (
            <>
              <p className="text-xs text-slate-500">Last check {when(check.created_at)}</p>
              <Signals signals={check.report.signals} />
            </>
          ) : (
            <Empty>No drift check has run yet.</Empty>
          )}
        </div>
      </Card>
      <Card title="Alerts">
        {s && s.open_alerts.length === 0 ? (
          <Empty>No alerts.</Empty>
        ) : (
          <ul className="space-y-2 text-sm">
            {s?.open_alerts.map((a) => (
              <li key={a.id} className="flex items-start gap-2">
                <Badge>{a.severity}</Badge>
                <Badge tone={a.evidence === "measured" ? "red" : "amber"}>{a.evidence}</Badge>
                <div>
                  <span className="font-mono">{a.category}.{a.metric}</span>
                  <div className="text-xs text-slate-500">{a.message} · {when(a.created_at)}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export default function Evaluations({ selected }: { selected?: string }) {
  const runs = useLoad(() => api.evalRuns());
  const [tab, setTab] = useState<"runs" | "monitoring">("runs");
  const forbidden = runs.error instanceof ApiError && runs.error.status === 403;
  const options = (runs.data ?? [])
    .filter((r) => r.status === "completed")
    .map((r) => ({
      id: r.id,
      label: `${when(r.started_at)} · ${r.config.profile ?? r.mode} · ${pct(r.pass_rate)} (${r.total_cases})`,
    }));
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Evaluations & Monitoring</h1>
        <div className="flex gap-1">
          <Button variant={tab === "runs" ? "primary" : "default"} onClick={() => setTab("runs")}>Evaluation runs</Button>
          <Button variant={tab === "monitoring" ? "primary" : "default"} onClick={() => setTab("monitoring")}>Monitoring</Button>
        </div>
      </div>
      {forbidden ? (
        <Card>
          <p className="text-sm">
            Evaluations and monitoring are operator functions: they run the shared model for minutes and read data across
            tenants. This API key's tenant is not an operator (set <span className="font-mono">AGENTOPS_ADMIN_TENANTS</span>{" "}
            on the server; in development <span className="font-mono">tenant_a</span> is one).
          </p>
        </Card>
      ) : tab === "monitoring" ? (
        <Monitoring />
      ) : (
        <div className="grid gap-4 xl:grid-cols-[22rem_1fr]">
          <div className="space-y-2">
            <ErrorBox error={runs.error} />
            {runs.data && runs.data.length === 0 && <Empty>No runs. Start one with `agentops eval run --judge`.</Empty>}
            <ul className="space-y-1">
              {runs.data?.map((r) => (
                <li key={r.id}>
                  <button
                    onClick={() => navigate("evaluations", r.id)}
                    className={cx(
                      "w-full rounded-md border px-3 py-2 text-left text-sm",
                      selected === r.id
                        ? "border-indigo-300 bg-indigo-50 dark:border-indigo-800 dark:bg-indigo-950"
                        : "border-slate-200 bg-white hover:bg-slate-50 dark:border-slate-800 dark:bg-slate-900 dark:hover:bg-slate-800",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <Badge>{r.status}</Badge>
                      <span>{r.config.profile ?? "default"}</span>
                      <span className="text-slate-500">{r.mode}</span>
                      <span className="ml-auto tabular-nums">{r.passed_cases}/{r.total_cases}</span>
                    </div>
                    <div className="mt-1 text-xs text-slate-500">{when(r.started_at)}</div>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className="space-y-4">
            <CompareRuns runs={options} />
            {selected ? <RunDetail id={selected} /> : <Empty>Select a run to see its scores.</Empty>}
          </div>
        </div>
      )}
    </div>
  );
}
