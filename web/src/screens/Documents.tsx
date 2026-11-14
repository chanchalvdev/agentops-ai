import { RefreshCw, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, Empty, ErrorBox, PageHead, Panel, Spinner, Status, cx, inputClass, useLoad } from "../components/ui";
import { api } from "../lib/api";
import { bytes, when } from "../lib/format";

const ACCEPT = ".txt,.md,.markdown,.pdf";

export default function Documents() {
  const docs = useLoad(() => api.documents());
  const [file, setFile] = useState<File | null>(null);
  const [metadata, setMetadata] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  // Indexing runs in the background on the server; poll while anything is still in flight.
  const inFlight = docs.data?.items.some((d) => d.status === "pending" || d.status === "processing");
  useEffect(() => {
    if (!inFlight) return;
    const t = setInterval(() => void docs.reload(), 2000);
    return () => clearInterval(t);
  }, [inFlight, docs.reload]);

  async function act(label: string, fn: () => Promise<unknown>, message?: string) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const result = await fn();
      const text = typeof result === "string" ? result : message;
      if (text) setNotice(text);
      await docs.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  }

  const upload = () => {
    if (!file) return;
    void act("upload", async () => {
      const r = await api.upload(file, metadata);
      setFile(null);
      if (input.current) input.current.value = "";
      return r.duplicate
        ? `${file.name} is already on record (identical content), so it was not indexed again.`
        : `${file.name} is uploaded. Chunking and embedding run in the background.`;
    });
  };

  const items = docs.data?.items ?? [];
  return (
    <div className="mx-auto max-w-6xl px-8 py-8">
      <PageHead
        title="Documents"
        sub="The corpus the agent may cite. Answers can only point at passages from documents that are ready."
      />

      <form
        className="mb-10 grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          upload();
        }}
      >
        <label
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            const f = e.dataTransfer.files[0];
            if (f) setFile(f);
          }}
          className={cx(
            "flex h-[68px] cursor-pointer items-center gap-3 border-1.5 border-dashed px-4 transition-colors",
            dragging ? "border-stamp bg-stamp-soft" : "border-ink/40 bg-paper hover:border-ink",
          )}
        >
          <Upload className="h-5 w-5 shrink-0 text-ink-2" aria-hidden />
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold">{file ? file.name : "Drop a file, or choose one"}</span>
            <span className="block text-xs text-ink-2">{file ? bytes(file.size) : "TXT, Markdown, or PDF with a text layer"}</span>
          </span>
          <input
            ref={input}
            type="file"
            accept={ACCEPT}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="sr-only"
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-xs font-semibold text-ink-2">Metadata, optional flat JSON</span>
          <input
            value={metadata}
            onChange={(e) => setMetadata(e.target.value)}
            placeholder='{"team": "payments"}'
            className={cx(inputClass, "h-[44px] font-mono text-xs")}
          />
        </label>
        <Button type="submit" variant="primary" disabled={!file} busy={busy === "upload"}>
          Upload
        </Button>
      </form>

      <div className="mb-6 space-y-3" aria-live="polite">
        {notice && <p className="border border-record/35 bg-record-soft px-4 py-2.5 text-sm text-record">{notice}</p>}
        <ErrorBox error={error ?? docs.error} />
      </div>

      <Panel
        title="On record"
        meta={docs.data ? `${docs.data.total} document${docs.data.total === 1 ? "" : "s"}` : undefined}
        actions={
          <Button size="sm" variant="ghost" onClick={() => void docs.reload()} busy={docs.loading && !!docs.data}>
            <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Refresh
          </Button>
        }
        flush
      >
        {!docs.data && docs.loading ? (
          <div className="py-10">
            <Spinner />
          </div>
        ) : items.length === 0 ? (
          <div className="pt-4">
            <Empty title="No documents yet">
              Upload one above, or index the sample corpus with <code className="font-mono">agentops ingest datasets/corpus</code>.
            </Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-rule text-left text-xs font-semibold text-ink-2">
                  <th className="py-2.5 pr-4 font-semibold">File</th>
                  <th className="px-4 font-semibold">Version</th>
                  <th className="px-4 font-semibold">Status</th>
                  <th className="px-4 text-right font-semibold">Chunks</th>
                  <th className="px-4 text-right font-semibold">Size</th>
                  <th className="px-4 font-semibold">Updated</th>
                  <th className="pl-4">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rule">
                {items.map((d) => (
                  <tr key={d.id} className="align-top hover:bg-wash/60">
                    <td className="max-w-[340px] py-3 pr-4">
                      <div className="truncate font-semibold" title={d.filename}>
                        {d.filename}
                      </div>
                      {Object.keys(d.metadata).length > 0 && (
                        <div className="mt-0.5 truncate font-mono text-xs text-ink-2">{JSON.stringify(d.metadata)}</div>
                      )}
                      {d.error && <div className="mt-0.5 text-xs text-seal">{d.error}</div>}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3">
                      <span className="font-mono">v{d.version}</span>
                      {d.is_latest && <span className="ml-2 text-xs text-ink-2">latest</span>}
                    </td>
                    <td className="px-4 py-3">
                      <Status value={d.status} />
                    </td>
                    <td className="px-4 py-3 text-right font-mono">{d.chunk_count}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-right font-mono">{bytes(d.size_bytes)}</td>
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-ink-2">{when(d.updated_at)}</td>
                    <td className="py-2 pl-4">
                      <div className="flex justify-end gap-1.5">
                        {confirming === d.id ? (
                          <>
                            <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                              Keep
                            </Button>
                            <Button
                              size="sm"
                              variant="danger"
                              busy={busy === `delete-${d.id}`}
                              onClick={() => void act(`delete-${d.id}`, () => api.deleteDocument(d.id), `Deleted ${d.filename} v${d.version} and its chunks.`)}
                            >
                              Delete v{d.version} and its chunks
                            </Button>
                          </>
                        ) : (
                          <>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={busy !== null}
                              busy={busy === `reindex-${d.id}`}
                              title="Re-chunk and re-embed with the current configuration"
                              onClick={() => void act(`reindex-${d.id}`, () => api.reindex(d.id), `Reindexing ${d.filename}.`)}
                            >
                              <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Reindex
                            </Button>
                            <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setConfirming(d.id)} title={`Delete ${d.filename}`}>
                              <Trash2 className="h-3.5 w-3.5" aria-hidden />
                              <span className="sr-only">Delete {d.filename}</span>
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
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
