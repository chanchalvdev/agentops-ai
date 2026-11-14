import { useEffect, useRef, useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Spinner, useLoad } from "../components/ui";
import { api } from "../lib/api";
import { bytes, when } from "../lib/format";

export default function Documents() {
  const docs = useLoad(() => api.documents());
  const [file, setFile] = useState<File | null>(null);
  const [metadata, setMetadata] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
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
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <h1 className="text-xl font-semibold">Documents</h1>
      <Card title="Upload">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!file) return;
            void act("upload", async () => {
              const r = await api.upload(file, metadata);
              setFile(null);
              if (input.current) input.current.value = "";
              return r.duplicate
                ? `${file.name} is already uploaded (identical content): not indexed again.`
                : `Uploaded ${file.name}; indexing runs in the background.`;
            });
          }}
        >
          <label className="text-sm">
            <span className="mb-1 block text-xs text-slate-500">TXT, Markdown, or PDF</span>
            <input
              ref={input}
              type="file"
              accept=".txt,.md,.markdown,.pdf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="text-sm"
            />
          </label>
          <label className="min-w-64 flex-1 text-sm">
            <span className="mb-1 block text-xs text-slate-500">Metadata (optional flat JSON)</span>
            <input
              value={metadata}
              onChange={(e) => setMetadata(e.target.value)}
              placeholder='{"team": "payments"}'
              className="w-full rounded-md border border-slate-300 bg-transparent px-2 py-1.5 font-mono text-xs dark:border-slate-700"
            />
          </label>
          <Button type="submit" variant="primary" disabled={!file || busy !== null}>
            {busy === "upload" ? <Spinner /> : null} Upload
          </Button>
        </form>
        {notice && <p className="mt-3 text-sm text-emerald-700 dark:text-emerald-400">{notice}</p>}
      </Card>
      <ErrorBox error={error ?? docs.error} />
      <Card
        title={`Your documents${docs.data ? ` (${docs.data.total})` : ""}`}
        actions={<Button onClick={() => void docs.reload()}>{docs.loading ? <Spinner /> : null} Refresh</Button>}
      >
        {docs.data && docs.data.items.length === 0 ? (
          <Empty>No documents yet. Upload one above, or run `agentops ingest datasets/corpus`.</Empty>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr>
                <th className="py-1">File</th>
                <th>Version</th>
                <th>Status</th>
                <th className="text-right">Chunks</th>
                <th className="text-right">Size</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {docs.data?.items.map((d) => (
                <tr key={d.id} className="border-t border-slate-100 align-top dark:border-slate-800">
                  <td className="py-2">
                    <div className="font-medium">{d.filename}</div>
                    {Object.keys(d.metadata).length > 0 && (
                      <div className="font-mono text-xs text-slate-500">{JSON.stringify(d.metadata)}</div>
                    )}
                    {d.error && <div className="text-xs text-rose-600">{d.error}</div>}
                  </td>
                  <td>
                    v{d.version} {d.is_latest ? <Badge tone="blue">latest</Badge> : null}
                  </td>
                  <td>
                    <Badge>{d.status}</Badge>
                  </td>
                  <td className="text-right tabular-nums">{d.chunk_count}</td>
                  <td className="text-right tabular-nums">{bytes(d.size_bytes)}</td>
                  <td className="text-xs text-slate-500">{when(d.updated_at)}</td>
                  <td className="space-x-1 whitespace-nowrap text-right">
                    <Button
                      disabled={busy !== null}
                      onClick={() => void act(`reindex-${d.id}`, () => api.reindex(d.id), `Reindexing ${d.filename}.`)}
                      title="Re-chunk and re-embed with the current configuration"
                    >
                      Reindex
                    </Button>
                    <Button
                      variant="danger"
                      disabled={busy !== null}
                      onClick={() => {
                        if (window.confirm(`Delete ${d.filename} v${d.version} and its chunks?`))
                          void act(`delete-${d.id}`, () => api.deleteDocument(d.id), `Deleted ${d.filename}.`);
                      }}
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
