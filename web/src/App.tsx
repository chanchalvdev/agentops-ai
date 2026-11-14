import { Component, useCallback, useEffect, useState, type ReactNode } from "react";
import { Badge, Button, cx } from "./components/ui";
import { api, getApiKey, setApiKey } from "./lib/api";
import Approvals from "./screens/Approvals";
import Chat from "./screens/Chat";
import Documents from "./screens/Documents";
import Evaluations from "./screens/Evaluations";
import Executions from "./screens/Executions";
import Workspace from "./screens/Workspace";

const SCREENS = [
  { id: "chat", label: "Chat" },
  { id: "documents", label: "Documents" },
  { id: "workspace", label: "Workspace" },
  { id: "approvals", label: "Approvals" },
  { id: "executions", label: "Executions" },
  { id: "evaluations", label: "Evaluations & Monitoring" },
] as const;
type ScreenId = (typeof SCREENS)[number]["id"];

function parseHash(): { screen: ScreenId; arg?: string } {
  const [, screen, arg] = location.hash.split("/");
  const found = SCREENS.find((s) => s.id === screen);
  return { screen: found ? found.id : "chat", arg: arg ? decodeURIComponent(arg) : undefined };
}

function Connection({ apiKey, onKey }: { apiKey: string; onKey: (k: string) => void }) {
  const [draft, setDraft] = useState(apiKey);
  const [state, setState] = useState<{ tenant?: string; ready?: string; error?: string }>({});

  const check = useCallback(async () => {
    const next: typeof state = {};
    try {
      const r = await api.ready();
      next.ready = r.status;
    } catch {
      next.ready = "unreachable";
    }
    if (apiKey) {
      try {
        next.tenant = (await api.whoami()).tenant_id;
      } catch (e) {
        next.error = e instanceof Error ? e.message : "invalid key";
      }
    }
    setState(next);
  }, [apiKey]);

  useEffect(() => {
    void check();
  }, [check]);

  return (
    <div className="space-y-2 border-t border-slate-200 p-3 text-xs dark:border-slate-800">
      <label className="block font-medium text-slate-600 dark:text-slate-400" htmlFor="api-key">
        API key
      </label>
      <form
        className="flex gap-1"
        onSubmit={(e) => {
          e.preventDefault();
          onKey(draft.trim());
        }}
      >
        <input
          id="api-key"
          type="password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="dev-key-tenant-a"
          className="min-w-0 flex-1 rounded border border-slate-300 bg-white px-2 py-1 dark:border-slate-700 dark:bg-slate-950"
        />
        <Button type="submit">Save</Button>
      </form>
      <div className="flex flex-wrap items-center gap-1.5">
        <span>API</span>
        <Badge tone={state.ready === "ready" ? "green" : state.ready ? "amber" : "gray"}>{state.ready ?? "…"}</Badge>
        {state.tenant && (
          <>
            <span>tenant</span>
            <Badge tone="blue">{state.tenant}</Badge>
          </>
        )}
      </div>
      {!apiKey && <p className="text-amber-700 dark:text-amber-400">Enter your API key to start.</p>}
      {state.error && <p className="text-rose-600">{state.error}</p>}
    </div>
  );
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
      <div className="rounded-md border border-rose-300 bg-rose-50 p-4 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300">
        This screen failed to render: {this.state.error.message}
      </div>
    );
  }
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [apiKey, setKey] = useState(getApiKey);
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
    <div className="flex h-screen">
      <nav className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="px-4 py-4">
          <div className="text-base font-bold tracking-tight">AgentOps AI</div>
          <div className="text-xs text-slate-500">local agentic RAG</div>
        </div>
        <ul className="flex-1 space-y-0.5 px-2">
          {SCREENS.map((s) => (
            <li key={s.id}>
              <a
                href={`#/${s.id}`}
                className={cx(
                  "block rounded-md px-3 py-1.5 text-sm",
                  route.screen === s.id
                    ? "bg-indigo-50 font-medium text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300"
                    : "text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
                )}
              >
                {s.label}
              </a>
            </li>
          ))}
        </ul>
        <Connection apiKey={apiKey} onKey={saveKey} />
      </nav>
      <main className="min-w-0 flex-1 overflow-y-auto p-6" key={`${apiKey}:${route.screen}`}>
        <ScreenBoundary>
        {route.screen === "chat" && <Chat />}
        {route.screen === "documents" && <Documents />}
        {route.screen === "workspace" && <Workspace />}
        {route.screen === "approvals" && <Approvals />}
        {route.screen === "executions" && <Executions selected={route.arg} />}
        {route.screen === "evaluations" && <Evaluations selected={route.arg} />}
        </ScreenBoundary>
      </main>
    </div>
  );
}
