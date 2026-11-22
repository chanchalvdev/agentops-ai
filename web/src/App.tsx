import { Activity, Files, FolderTree, KeyRound, MessageSquareText, ScrollText, Stamp, type LucideIcon } from "lucide-react";
import { Component, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { cx } from "./components/ui";
import { api, getApiKey, setApiKey } from "./lib/api";
import type { ScreenId } from "./lib/nav";
import Approvals from "./screens/Approvals";
import Chat from "./screens/Chat";
import Documents from "./screens/Documents";
import Evaluations from "./screens/Evaluations";
import Executions from "./screens/Executions";
import Workspace from "./screens/Workspace";

const SCREENS: { id: ScreenId; label: string; icon: LucideIcon }[] = [
  { id: "chat", label: "Ask", icon: MessageSquareText },
  { id: "documents", label: "Documents", icon: Files },
  { id: "workspace", label: "Workspace", icon: FolderTree },
  { id: "approvals", label: "Approvals", icon: Stamp },
  { id: "executions", label: "Executions", icon: ScrollText },
  { id: "evaluations", label: "Evals & drift", icon: Activity },
];

function parseHash(): { screen: ScreenId; arg?: string } {
  const [, screen, arg] = location.hash.split("/");
  const found = SCREENS.find((s) => s.id === screen);
  return { screen: found ? found.id : "chat", arg: arg ? decodeURIComponent(arg) : undefined };
}

/** One broken screen must not blank the whole app: show the error and keep the navigation. */
class ScreenBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="border border-seal/35 bg-seal-soft p-4 text-sm text-seal">
        This screen failed to render: {this.state.error.message}
      </div>
    );
  }
}

const WAKE_ATTEMPTS = 20; // x WAKE_RETRY_MS: about a minute, longer than a cold start
const WAKE_RETRY_MS = 3000;

function Connection({ apiKey, onKey }: { apiKey: string; onKey: (k: string) => void }) {
  const [draft, setDraft] = useState(apiKey);
  const [state, setState] = useState<{ tenant?: string; ready?: string; error?: string }>({});
  // A hosted API that scales to zero takes a while to start: say so instead of showing an error.
  const [waking, setWaking] = useState(false);

  const check = useCallback(
    async (alive: () => boolean) => {
      const slow = setTimeout(() => alive() && setWaking(true), 2500);
      const next: typeof state = {};
      for (let attempt = 0; attempt < WAKE_ATTEMPTS && alive(); attempt++) {
        try {
          next.ready = (await api.ready()).status;
        } catch {
          next.ready = "unreachable";
        }
        if (next.ready === "ready") break;
        setWaking(true);
        await new Promise((resolve) => setTimeout(resolve, WAKE_RETRY_MS));
      }
      clearTimeout(slow);
      if (!alive()) return;
      if (apiKey) {
        try {
          next.tenant = (await api.whoami()).tenant_id;
        } catch (e) {
          next.error = e instanceof Error ? e.message : "invalid key";
        }
      }
      setWaking(false);
      setState(next);
    },
    [apiKey],
  );

  useEffect(() => {
    let alive = true;
    void check(() => alive);
    return () => {
      alive = false;
    };
  }, [check]);

  const ok = state.ready === "ready";
  return (
    <div className="border-t-1.5 border-ink px-4 py-4">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold text-manila-ink">
        <span className={cx("h-2 w-2", ok ? "bg-record" : state.ready ? "bg-seal" : "bg-manila-ink/40")} aria-hidden />
        <span>API {waking ? "waking up…" : (state.ready ?? "checking…")}</span>
        {state.tenant && <span className="ml-auto font-mono text-ink">{state.tenant}</span>}
      </div>
      <form
        className="flex"
        onSubmit={(e) => {
          e.preventDefault();
          onKey(draft.trim());
        }}
      >
        <label htmlFor="api-key" className="sr-only">
          API key
        </label>
        <div className="relative min-w-0 flex-1">
          <KeyRound className="pointer-events-none absolute left-2 top-2 h-4 w-4 text-manila-ink" aria-hidden />
          <input
            id="api-key"
            type="password"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="API key"
            autoComplete="off"
            className="h-8 w-full border-1.5 border-ink bg-manila-hover/60 pl-7 pr-2 text-xs text-ink placeholder:text-manila-ink focus:bg-paper focus:outline-none"
          />
        </div>
        <button type="submit" className="h-8 border-1.5 border-l-0 border-ink bg-ink px-2.5 text-xs font-semibold text-paper hover:bg-[#25304a]">
          Use
        </button>
      </form>
      {waking && (
        <p className="mt-2 text-xs font-semibold text-ink" role="status">
          The server sleeps when idle and is starting up. This takes about 20 seconds.
        </p>
      )}
      {!apiKey && <p className="mt-2 text-xs font-semibold text-ink">Enter your API key to start (dev: dev-key-tenant-a).</p>}
      {state.error && <p className="mt-2 text-xs font-semibold text-seal">{state.error}</p>}
    </div>
  );
}

interface Counts {
  approvals: number;
  documents: number | null;
  runs: number | null; // executions in the last 24 h
}

/** The rail's counts: pending approvals (polled), documents on record, and runs in 24 h. */
function useCounts(apiKey: string): Counts {
  const [counts, setCounts] = useState<Counts>({ approvals: 0, documents: null, runs: null });
  useEffect(() => {
    if (!apiKey) return;
    let alive = true;
    const poll = async () => {
      const [approvals, documents, runs] = await Promise.allSettled([
        api.approvals("pending"),
        api.documents(),
        api.whoami(),
      ]);
      if (!alive) return;
      setCounts({
        approvals: approvals.status === "fulfilled" ? approvals.value.length : 0,
        documents: documents.status === "fulfilled" ? documents.value.total : null,
        runs: runs.status === "fulfilled" ? runs.value.executions : null,
      });
    };
    void poll();
    const t = setInterval(poll, 15_000);
    const onEvent = () => void poll();
    for (const name of ["focus", "agentops:approvals", "agentops:counts"]) window.addEventListener(name, onEvent);
    return () => {
      alive = false;
      clearInterval(t);
      for (const name of ["focus", "agentops:approvals", "agentops:counts"]) window.removeEventListener(name, onEvent);
    };
  }, [apiKey]);
  return counts;
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [apiKey, setKey] = useState(getApiKey);
  const counts = useCounts(apiKey);
  const activeItem = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    // On the narrow top bar the nav scrolls; keep the current screen visible, never under the fade.
    activeItem.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [route.screen]);
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const saveKey = (k: string) => {
    setApiKey(k);
    setKey(k);
  };

  return (
    <div className="flex h-full flex-col md:grid md:grid-cols-[232px_minmax(0,1fr)]">
      <nav className="flex shrink-0 flex-col bg-manila text-ink md:min-h-0" aria-label="Primary">
        <div className="flex items-center justify-between gap-3 border-b-1.5 border-ink px-5 py-3 md:block md:pb-4 md:pt-5">
          <div>
            <div className="text-xl font-extrabold leading-none">AgentOps AI</div>
            <div className="mt-1.5 hidden text-xs font-semibold text-manila-ink md:block">Every answer on the record</div>
          </div>
          <details className="relative md:hidden">
            <summary className="cursor-pointer list-none text-xs font-semibold underline">Connection</summary>
            <div className="absolute right-0 top-7 z-30 w-72 border-1.5 border-ink bg-manila">
              <Connection apiKey={apiKey} onKey={saveKey} />
            </div>
          </details>
        </div>
        <ul className="flex scroll-px-24 gap-0.5 overflow-x-auto px-2.5 py-2 pr-10 [mask-image:linear-gradient(to_right,black_82%,transparent)] md:pr-2.5 md:[mask-image:none] md:flex-1 md:flex-col md:space-y-0.5 md:overflow-y-auto md:py-3">
          {SCREENS.map(({ id, label, icon: Icon }) => {
            const on = route.screen === id;
            return (
              <li key={id}>
                <a
                  href={`#/${id}`}
                  ref={on ? activeItem : undefined}
                  aria-current={on ? "page" : undefined}
                  className={cx(
                    "flex h-9 items-center gap-2.5 whitespace-nowrap px-2.5 text-md font-semibold transition-colors duration-150",
                    on ? "bg-ink text-paper" : "text-ink hover:bg-manila-hover",
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" strokeWidth={2} aria-hidden />
                  <span className="md:flex-1">{label}</span>
                  {id === "approvals" && counts.approvals > 0 && (
                    <span
                      className="min-w-5 bg-stamp px-1.5 text-center font-mono text-xs font-bold leading-5 text-white"
                      aria-label={`${counts.approvals} pending`}
                    >
                      {counts.approvals}
                    </span>
                  )}
                  {id === "documents" && counts.documents != null && (
                    <span className={cx("font-mono text-xs", on ? "text-paper/70" : "text-manila-ink")} aria-label={`${counts.documents} on record`}>
                      {counts.documents}
                    </span>
                  )}
                  {id === "executions" && counts.runs != null && (
                    <span
                      className={cx("font-mono text-xs", on ? "text-paper/70" : "text-manila-ink")}
                      title="Runs in the last 24 hours"
                      aria-label={`${counts.runs} runs in the last 24 hours`}
                    >
                      {counts.runs}
                    </span>
                  )}
                </a>
              </li>
            );
          })}
        </ul>
        <div className="hidden md:block">
          <Connection apiKey={apiKey} onKey={saveKey} />
        </div>
      </nav>
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto" key={`${apiKey}:${route.screen}`}>
        <ScreenBoundary>
          {route.screen === "chat" && <Chat openId={route.arg} />}
          {route.screen === "documents" && <Documents />}
          {route.screen === "workspace" && <Workspace initialPath={route.arg} />}
          {route.screen === "approvals" && <Approvals />}
          {route.screen === "executions" && <Executions selected={route.arg} />}
          {route.screen === "evaluations" && <Evaluations selected={route.arg} />}
        </ScreenBoundary>
      </main>
    </div>
  );
}
