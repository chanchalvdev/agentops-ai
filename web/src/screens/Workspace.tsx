import { useEffect, useRef, useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Spinner, cx, useLoad } from "../components/ui";
import { api, openTerminal } from "../lib/api";
import { bytes, ms, when } from "../lib/format";
import type { Entry, FileContent } from "../lib/types";

function join(dir: string, name: string): string {
  return dir === "." ? name : `${dir}/${name}`;
}

function Dir({ path, depth, onOpen, selected }: { path: string; depth: number; onOpen: (p: string) => void; selected: string | null }) {
  const list = useLoad(() => api.tree(path), [path]);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (list.error) return <div className="pl-2"><ErrorBox error={list.error} /></div>;
  if (!list.data) return <div className="py-1 pl-4"><Spinner /></div>;
  const entries = [...list.data.entries].sort((a: Entry, b: Entry) =>
    a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1,
  );
  return (
    <ul>
      {entries.map((e) => {
        const p = join(path, e.name);
        return (
          <li key={p}>
            <button
              className={cx(
                "flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800",
                selected === p && "bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300",
              )}
              style={{ paddingLeft: depth * 12 + 4 }}
              onClick={() => (e.type === "dir" ? setOpen((o) => ({ ...o, [p]: !o[p] })) : onOpen(p))}
            >
              <span className="w-3 text-slate-400">{e.type === "dir" ? (open[p] ? "▾" : "▸") : ""}</span>
              <span className={e.type === "dir" ? "font-medium" : ""}>{e.name}</span>
              {e.type === "file" && <span className="ml-auto text-xs text-slate-400">{bytes(e.size)}</span>}
            </button>
            {e.type === "dir" && open[p] && <Dir path={p} depth={depth + 1} onOpen={onOpen} selected={selected} />}
          </li>
        );
      })}
      {list.data.truncated && <li className="pl-4 text-xs text-slate-500">… truncated</li>}
      {entries.length === 0 && <li className="pl-4 text-xs text-slate-500">(empty)</li>}
    </ul>
  );
}

type Line = { kind: "cmd" | "out" | "exit" | "error" | "info"; text: string };

function Terminal() {
  const [lines, setLines] = useState<Line[]>([{ kind: "info", text: "Connecting…" }]);
  const [input, setInput] = useState("");
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const ws = useRef<WebSocket | null>(null);
  const pane = useRef<HTMLDivElement>(null);
  const add = (l: Line) => setLines((all) => [...all.slice(-500), l]);

  useEffect(() => {
    const socket = openTerminal();
    ws.current = socket;
    socket.onmessage = (m) => {
      const msg = JSON.parse(String(m.data)) as Record<string, unknown>;
      if (msg.type === "ready") {
        setReady(true);
        add({ kind: "info", text: `Connected as ${String(msg.tenant)}. Allowlisted commands only; no shell, pipes, or redirects.` });
      } else if (msg.type === "output") add({ kind: "out", text: String(msg.data) });
      else if (msg.type === "exit") {
        setBusy(false);
        const code = msg.exit_code;
        add({
          kind: "exit",
          text: `exit ${code ?? "killed"}${msg.timed_out ? " (timed out)" : ""}${msg.truncated ? " (output truncated)" : ""} · ${ms(Number(msg.duration_ms))}`,
        });
      } else if (msg.type === "error") {
        setBusy(false);
        add({ kind: "error", text: `${String(msg.code)}: ${String(msg.message)}` });
      }
    };
    socket.onclose = (e) => {
      setReady(false);
      setBusy(false);
      const why = e.code === 4401 ? "unauthenticated (check the API key)" : e.code === 4403 ? "this tenant has no workspace permission" : `closed (${e.code})`;
      add({ kind: "error", text: `Terminal ${why}.` });
    };
    return () => {
      // Detach first: a socket closed by this cleanup must not report into the next session.
      socket.onmessage = null;
      socket.onclose = null;
      socket.close();
    };
  }, []);

  useEffect(() => {
    // Scroll the terminal pane only (scrollIntoView would also scroll the page).
    const el = pane.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const run = () => {
    const line = input.trim();
    if (!line || !ready || busy) return;
    add({ kind: "cmd", text: `$ ${line}` });
    setBusy(true);
    setInput("");
    ws.current?.send(JSON.stringify({ line }));
  };

  return (
    <div className="flex h-72 flex-col rounded-md bg-slate-950 font-mono text-xs text-slate-200">
      <div ref={pane} className="flex-1 overflow-y-auto p-3">
        {lines.map((l, i) => (
          <div
            key={i}
            className={cx(
              "whitespace-pre-wrap break-words",
              l.kind === "cmd" && "mt-1 text-sky-300",
              l.kind === "exit" && "text-slate-500",
              l.kind === "error" && "text-rose-400",
              l.kind === "info" && "text-slate-500",
            )}
          >
            {l.text}
          </div>
        ))}
      </div>
      <form
        className="flex border-t border-slate-800"
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
      >
        <span className="px-3 py-2 text-sky-300">$</span>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={!ready}
          placeholder={ready ? (busy ? "running…" : "git status · ls calc · python -m pytest") : "not connected"}
          className="flex-1 bg-transparent py-2 pr-3 outline-none"
        />
      </form>
    </div>
  );
}

export default function Workspace() {
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<FileContent | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [treeKey, setTreeKey] = useState(0);
  const [showAudit, setShowAudit] = useState(false);
  const audit = useLoad(() => (showAudit ? api.audit(50) : Promise.resolve([])), [showAudit, treeKey]);

  async function open(path: string) {
    setSelected(path);
    setError(null);
    setSaved(null);
    try {
      const f = await api.readFile(path);
      setFile(f);
      setDraft(f.content);
    } catch (e) {
      setFile(null);
      setError(e);
    }
  }

  async function save() {
    if (!file) return;
    setError(null);
    try {
      const r = await api.writeFile(file.path, draft);
      setFile({ ...file, content: draft });
      setSaved(`Saved ${r.bytes} bytes.`);
      setTreeKey((k) => k + 1);
    } catch (e) {
      setError(e);
    }
  }

  const dirty = file !== null && draft !== file.content;
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Workspace</h1>
      <p className="text-sm text-slate-500">
        Your sandboxed directory. Edits you make here are yours; changes the agent proposes wait in Approvals.
      </p>
      <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
        <Card title="Files" actions={<Button variant="ghost" onClick={() => setTreeKey((k) => k + 1)}>Refresh</Button>}>
          <div className="max-h-[28rem] overflow-y-auto">
            <Dir key={treeKey} path="." depth={0} onOpen={(p) => void open(p)} selected={selected} />
          </div>
        </Card>
        <Card
          title={selected ?? "No file selected"}
          actions={
            file && (
              <>
                {file.truncated && <Badge tone="amber">truncated: read-only</Badge>}
                <Button variant="primary" disabled={!dirty || file.truncated} onClick={() => void save()}>
                  Save
                </Button>
              </>
            )
          }
        >
          <ErrorBox error={error} />
          {saved && <p className="mb-2 text-sm text-emerald-700 dark:text-emerald-400">{saved}</p>}
          {file ? (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              readOnly={file.truncated}
              spellCheck={false}
              className="h-[26rem] w-full resize-y rounded-md border border-slate-300 bg-transparent p-3 font-mono text-xs dark:border-slate-700"
            />
          ) : (
            !error && <Empty>Pick a file on the left.</Empty>
          )}
        </Card>
      </div>
      <Card title="Terminal">
        <Terminal />
      </Card>
      <Card
        title="Audit log"
        actions={<Button variant="ghost" onClick={() => setShowAudit((s) => !s)}>{showAudit ? "Hide" : "Show"}</Button>}
      >
        {showAudit ? (
          <>
            <ErrorBox error={audit.error} />
            <table className="w-full text-xs">
              <thead className="text-left text-slate-500">
                <tr><th>When</th><th>Actor</th><th>Operation</th><th>Target</th><th>Status</th></tr>
              </thead>
              <tbody>
                {audit.data?.map((a) => (
                  <tr key={a.id} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-1 text-slate-500">{when(a.created_at)}</td>
                    <td>{a.actor}</td>
                    <td>{a.operation}</td>
                    <td className="max-w-md truncate font-mono">{a.target}</td>
                    <td><Badge>{a.status}</Badge> {a.error_code ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p className="text-sm text-slate-500">Every file and command operation, by you or the agent, including refusals.</p>
        )}
      </Card>
    </div>
  );
}
