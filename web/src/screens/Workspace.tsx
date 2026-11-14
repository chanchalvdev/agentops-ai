import { ChevronDown, ChevronRight, FileText, Folder, FolderOpen, RefreshCw, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, Empty, ErrorBox, PageHead, Panel, Spinner, Status, cx, useLoad } from "../components/ui";
import { api, openTerminal } from "../lib/api";
import { bytes, ms, when } from "../lib/format";
import type { Entry, FileContent } from "../lib/types";

function join(dir: string, name: string): string {
  return dir === "." ? name : `${dir}/${name}`;
}

function Dir({ path, depth, onOpen, selected }: { path: string; depth: number; onOpen: (p: string) => void; selected: string | null }) {
  const list = useLoad(() => api.tree(path), [path]);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (list.error) return <div className="py-2"><ErrorBox error={list.error} /></div>;
  if (!list.data) return <div className="py-1.5" style={{ paddingLeft: depth * 14 + 8 }}><Spinner className="h-3.5 w-3.5" /></div>;
  const entries = [...list.data.entries].sort((a: Entry, b: Entry) =>
    a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1,
  );
  return (
    <ul role={depth === 0 ? "tree" : "group"}>
      {entries.map((e) => {
        const p = join(path, e.name);
        const isDir = e.type === "dir";
        const expanded = !!open[p];
        return (
          <li key={p} role="treeitem" aria-expanded={isDir ? expanded : undefined} aria-selected={selected === p}>
            <button
              className={cx(
                "flex h-8 w-full items-center gap-1.5 pr-2 text-left text-sm transition-colors",
                selected === p ? "bg-ink text-paper" : "hover:bg-wash",
              )}
              style={{ paddingLeft: depth * 14 + 6 }}
              onClick={() => (isDir ? setOpen((o) => ({ ...o, [p]: !o[p] })) : onOpen(p))}
            >
              {isDir ? (
                expanded ? <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
              ) : (
                <span className="w-3.5 shrink-0" />
              )}
              {isDir ? (
                expanded ? <FolderOpen className="h-4 w-4 shrink-0" aria-hidden /> : <Folder className="h-4 w-4 shrink-0" aria-hidden />
              ) : (
                <FileText className="h-4 w-4 shrink-0 opacity-70" aria-hidden />
              )}
              <span className={cx("min-w-0 flex-1 truncate", isDir && "font-semibold")}>{e.name}</span>
              {e.type === "file" && <span className={cx("font-mono text-2xs", selected === p ? "text-paper/70" : "text-ink-3")}>{bytes(e.size)}</span>}
            </button>
            {isDir && expanded && <Dir path={p} depth={depth + 1} onOpen={onOpen} selected={selected} />}
          </li>
        );
      })}
      {list.data.truncated && <li className="py-1 text-xs text-ink-2" style={{ paddingLeft: depth * 14 + 26 }}>… more entries not shown</li>}
      {entries.length === 0 && <li className="py-1 text-xs text-ink-2" style={{ paddingLeft: depth * 14 + 26 }}>empty</li>}
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
  const field = useRef<HTMLInputElement>(null);
  const add = (l: Line) => setLines((all) => [...all.slice(-500), l]);

  useEffect(() => {
    const socket = openTerminal();
    ws.current = socket;
    socket.onmessage = (m) => {
      const msg = JSON.parse(String(m.data)) as Record<string, unknown>;
      if (msg.type === "ready") {
        setReady(true);
        add({ kind: "info", text: `Connected as ${String(msg.tenant)}. Allowlisted commands only: no shell, pipes, or redirects.` });
      } else if (msg.type === "output") add({ kind: "out", text: String(msg.data) });
      else if (msg.type === "exit") {
        setBusy(false);
        add({
          kind: "exit",
          text: `exit ${msg.exit_code ?? "killed"}${msg.timed_out ? " · timed out" : ""}${msg.truncated ? " · output truncated" : ""} · ${ms(Number(msg.duration_ms))}`,
        });
      } else if (msg.type === "error") {
        setBusy(false);
        add({ kind: "error", text: `refused · ${String(msg.code).replace(/_/g, " ")}: ${String(msg.message)}` });
      }
    };
    socket.onclose = (e) => {
      setReady(false);
      setBusy(false);
      const why =
        e.code === 4401 ? "unauthenticated: check the API key" : e.code === 4403 ? "this tenant has no workspace permission" : `connection closed (${e.code})`;
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
    const el = pane.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const run = () => {
    const line = input.trim();
    if (!line || !ready || busy) return;
    add({ kind: "cmd", text: line });
    setBusy(true);
    setInput("");
    ws.current?.send(JSON.stringify({ line }));
  };

  return (
    <div className="flex h-80 flex-col bg-ink font-mono text-xs text-[#E6E8E2]" onClick={() => field.current?.focus()}>
      <div ref={pane} className="flex-1 overflow-y-auto px-4 py-3 leading-relaxed" aria-live="polite">
        {lines.map((l, i) => (
          <div
            key={i}
            className={cx(
              "whitespace-pre-wrap break-words",
              l.kind === "cmd" && "mt-2 text-[#C9B8F2]",
              l.kind === "exit" && "text-[#8C94A6]",
              l.kind === "error" && "text-[#FF9EA4]",
              l.kind === "info" && "text-[#8C94A6]",
            )}
          >
            {l.kind === "cmd" ? `$ ${l.text}` : l.text}
          </div>
        ))}
      </div>
      <form
        className="flex items-center border-t border-white/10"
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
      >
        <span className="pl-4 pr-2 text-[#C9B8F2]" aria-hidden>
          $
        </span>
        <label htmlFor="terminal-input" className="sr-only">
          Command
        </label>
        <input
          id="terminal-input"
          ref={field}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={!ready}
          autoComplete="off"
          spellCheck={false}
          placeholder={ready ? (busy ? "running…" : "git status · ls calc · python -m pytest") : "not connected"}
          className="h-10 flex-1 bg-transparent pr-4 text-[#F2F3EF] placeholder:text-[#8C94A6] focus:outline-none"
          style={{ caretColor: "#C9B8F2" }}
        />
        {busy && <Spinner className="mr-4 h-3.5 w-3.5 text-[#C9B8F2]" />}
      </form>
    </div>
  );
}

export default function Workspace({ initialPath }: { initialPath?: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<FileContent | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [treeKey, setTreeKey] = useState(0);
  const audit = useLoad(() => api.audit(50), [treeKey]);

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
    setSaving(true);
    try {
      const r = await api.writeFile(file.path, draft);
      setFile({ ...file, content: draft });
      setSaved(`Saved ${r.bytes.toLocaleString()} bytes.`);
      setTreeKey((k) => k + 1);
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    if (initialPath) void open(initialPath); // deep link: #/workspace/<path>
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPath]);

  const dirty = file !== null && draft !== file.content;
  return (
    <div className="mx-auto max-w-[1400px] px-8 py-8">
      <PageHead
        title="Workspace"
        sub="Your sandboxed directory. Your own edits save directly; changes the agent proposes wait in Approvals."
      />
      <div className="grid gap-8 lg:grid-cols-[300px_minmax(0,1fr)]">
        <Panel
          title="Files"
          actions={
            <Button size="sm" variant="ghost" onClick={() => setTreeKey((k) => k + 1)} title="Reload the tree">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden />
              <span className="sr-only">Reload</span>
            </Button>
          }
          flush
        >
          <div className="max-h-[460px] overflow-y-auto py-2">
            <Dir key={treeKey} path="." depth={0} onOpen={(p) => void open(p)} selected={selected} />
          </div>
        </Panel>
        <Panel
          title={<span className="font-mono text-md">{selected ?? "No file open"}</span>}
          meta={file ? `${bytes(file.size)}${dirty ? " · unsaved changes" : ""}` : undefined}
          actions={
            file && (
              <>
                {file.truncated && <Status value="read only: truncated" tone="pending" />}
                {saved && !dirty && <span className="text-xs font-semibold text-record">{saved}</span>}
                <Button size="sm" variant="primary" disabled={!dirty || file.truncated} busy={saving} onClick={() => void save()}>
                  <Save className="h-3.5 w-3.5" aria-hidden /> Save
                </Button>
              </>
            )
          }
          flush
        >
          <div className="pt-3">
            <ErrorBox error={error} />
            {file ? (
              <textarea
                aria-label={`Contents of ${file.path}`}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                    e.preventDefault();
                    if (dirty && !file.truncated) void save();
                  }
                }}
                readOnly={file.truncated}
                spellCheck={false}
                className="mt-1 h-[440px] w-full resize-y border-1.5 border-ink/25 bg-paper p-4 font-mono text-xs leading-relaxed text-ink focus:border-stamp focus:outline-none"
              />
            ) : (
              !error && <Empty title="Pick a file in the tree">It opens here for reading and editing. Cmd/Ctrl+S saves.</Empty>
            )}
          </div>
        </Panel>
      </div>

      <Panel title="Terminal" meta="The same sandbox and allowlist as the agent's workspace_exec tool" className="mt-10" flush>
        <div className="pt-3">
          <Terminal />
        </div>
      </Panel>

      <Panel
        title="Audit log"
        meta="Every file and command operation, by you or the agent, including refusals"
        className="mt-10"
        actions={
          <Button size="sm" variant="ghost" onClick={() => void audit.reload()} busy={audit.loading && !!audit.data}>
            <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Refresh
          </Button>
        }
        flush
      >
        <ErrorBox error={audit.error} />
        {audit.data && audit.data.length === 0 ? (
          <div className="pt-4">
            <Empty title="Nothing recorded yet" />
          </div>
        ) : (
          <div className="max-h-[380px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-ground">
                <tr className="border-b border-rule text-left text-xs text-ink-2">
                  <th className="py-2.5 pr-4 font-semibold">When</th>
                  <th className="px-4 font-semibold">Actor</th>
                  <th className="px-4 font-semibold">Operation</th>
                  <th className="px-4 font-semibold">Target</th>
                  <th className="px-4 font-semibold">Outcome</th>
                  <th className="pl-4 text-right font-semibold">Time</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rule">
                {audit.data?.map((a) => (
                  <tr key={a.id} className="hover:bg-wash/60">
                    <td className="whitespace-nowrap py-2 pr-4 font-mono text-xs text-ink-2">{when(a.created_at)}</td>
                    <td className="px-4">{a.actor}</td>
                    <td className="px-4 font-mono text-xs">{a.operation}</td>
                    <td className="max-w-[380px] truncate px-4 font-mono text-xs" title={a.target}>
                      {a.target}
                    </td>
                    <td className="whitespace-nowrap px-4">
                      <Status value={a.status} />
                      {a.error_code && <span className="ml-2 text-xs text-ink-2">{a.error_code.replace(/_/g, " ")}</span>}
                    </td>
                    <td className="whitespace-nowrap pl-4 text-right font-mono text-xs text-ink-2">{ms(a.duration_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
