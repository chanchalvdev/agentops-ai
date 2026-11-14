import { ArrowUpRight } from "lucide-react";
import { useState } from "react";
import { Code, ErrorBox, Sheet, Spinner, cx } from "./ui";
import { api } from "../lib/api";
import { splitCitations } from "../lib/format";
import type { ChunkOut, Citation, ToolReference } from "../lib/types";

/** A source excerpt as clean quoted text: markdown heading markers dropped, list items kept on
 * their own lines. Display only; the full chunk opens in the sheet. */
export function cleanSnippet(text: string): string {
  return text
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\s+-\s+(?=[A-Z(])/g, "\n– ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// An answer and its exhibits: inline [S1]/[T1] chips, numbered exhibits beside it, and the
// source passage or tool result in a side sheet. Focusing a chip or an exhibit keeps that one at
// full ink and drops the rest back, so the reader sees exactly which passage backs which claim.

export function useEvidence() {
  const [focus, setFocus] = useState<string | null>(null);
  const [chunk, setChunk] = useState<{ citation: Citation; data?: ChunkOut; error?: unknown } | null>(null);
  const [tool, setTool] = useState<ToolReference | null>(null);
  const openCitation = async (c: Citation) => {
    setChunk({ citation: c });
    try {
      setChunk({ citation: c, data: await api.chunk(c.document_id, c.chunk_id) });
    } catch (error) {
      setChunk({ citation: c, error });
    }
  };
  const sheets = (
    <>
      <Sheet
        open={!!chunk}
        onClose={() => setChunk(null)}
        title={chunk ? `${chunk.citation.source_id} · ${chunk.citation.filename}` : ""}
        meta={
          chunk &&
          [
            chunk.citation.section ? `§ ${chunk.citation.section}` : null,
            chunk.data ? `chunk ${chunk.data.chunk_index}` : null,
            chunk.data ? `version ${chunk.data.document_version}` : null,
          ]
            .filter(Boolean)
            .join(" · ")
        }
      >
        {chunk && (
          <div className="space-y-4">
            <ErrorBox error={chunk.error} />
            {chunk.data ? <p className="whitespace-pre-wrap text-md leading-relaxed">{chunk.data.text}</p> : !chunk.error && <Spinner />}
          </div>
        )}
      </Sheet>
      <Sheet open={!!tool} onClose={() => setTool(null)} title={tool ? `${tool.source_id} · ${tool.tool}` : ""} meta="Tool result, as recorded">
        {tool && <Code>{JSON.stringify(tool.output, null, 2)}</Code>}
      </Sheet>
    </>
  );
  return { focus, setFocus, openCitation: (c: Citation) => void openCitation(c), openTool: setTool, sheets };
}

export function AnswerText({
  answer,
  citations,
  tools,
  onFocus,
  onOpenCitation,
  onOpenTool,
}: {
  answer: string;
  citations: Citation[];
  tools: ToolReference[];
  onFocus: (id: string | null) => void;
  onOpenCitation: (c: Citation) => void;
  onOpenTool: (t: ToolReference) => void;
}) {
  const open = (id: string) => {
    const c = citations.find((x) => x.source_id === id);
    if (c) return onOpenCitation(c);
    const t = tools.find((x) => x.source_id === id);
    if (t) onOpenTool(t);
  };
  const known = new Set([...citations.map((c) => c.source_id), ...tools.map((t) => t.source_id)]);
  return (
    <p className="max-w-[68ch] whitespace-pre-wrap text-xl font-normal leading-[1.55] tracking-[-0.005em]">
      {splitCitations(answer).map((seg, i) =>
        typeof seg === "string" ? (
          <span key={i}>{seg}</span>
        ) : known.has(seg.label) ? (
          <button
            key={i}
            onClick={() => open(seg.label)}
            onMouseEnter={() => onFocus(seg.label)}
            onMouseLeave={() => onFocus(null)}
            onFocus={() => onFocus(seg.label)}
            onBlur={() => onFocus(null)}
            className="mx-0.5 bg-ink px-1.5 align-[2px] font-mono text-xs font-bold text-paper transition-colors hover:bg-stamp"
            aria-label={`Open exhibit ${seg.label}`}
          >
            {seg.label}
          </button>
        ) : (
          <span key={i} className="font-mono text-sm text-ink-2" title="Recorded in the ledger, not as an exhibit">
            [{seg.label}]
          </span>
        ),
      )}
    </p>
  );
}

export function Exhibits({
  citations,
  tools,
  focus,
  onFocus,
  onOpenCitation,
  onOpenTool,
  note,
}: {
  citations: Citation[];
  tools: ToolReference[];
  focus: string | null;
  onFocus: (id: string | null) => void;
  onOpenCitation: (c: Citation) => void;
  onOpenTool: (t: ToolReference) => void;
  note?: string;
}) {
  const items = [
    ...citations.map((c) => ({
      id: c.source_id,
      title: c.filename,
      where: [c.section ? `§ ${c.section}` : null, c.page_start ? `p. ${c.page_start}` : null].filter(Boolean).join(" · "),
      body: cleanSnippet(c.snippet),
      open: () => onOpenCitation(c),
      mono: false,
    })),
    ...tools.map((t) => ({
      id: t.source_id,
      title: t.tool,
      where: "tool result",
      body: JSON.stringify(t.output),
      open: () => onOpenTool(t),
      mono: true,
    })),
  ];
  return (
    <div>
      <h2 className="mb-3 text-md font-bold">
        Exhibits <span className="font-mono text-sm font-normal text-ink-2">{items.length}</span>
      </h2>
      {items.length === 0 ? (
        <p className="text-sm text-ink-2">No exhibits: this answer cites nothing.</p>
      ) : (
        <ol className="space-y-3" onMouseLeave={() => onFocus(null)}>
          {items.map((x) => {
            const dim = focus !== null && focus !== x.id;
            return (
              <li key={x.id}>
                <button
                  onClick={x.open}
                  onMouseEnter={() => onFocus(x.id)}
                  onFocus={() => onFocus(x.id)}
                  onBlur={() => onFocus(null)}
                  className={cx(
                    "group block w-full border-1.5 bg-paper text-left transition-[opacity,border-color,box-shadow] duration-200 ease-out",
                    focus === x.id ? "border-stamp shadow-lift" : "border-ink/25 hover:border-ink",
                    dim && "opacity-45",
                  )}
                >
                  <div className="flex items-center gap-2 border-b border-rule px-3 py-2">
                    <span className="bg-ink px-1.5 font-mono text-xs font-bold text-paper">{x.id}</span>
                    <span className="min-w-0 flex-1 truncate text-sm font-semibold">{x.title}</span>
                    <ArrowUpRight className="h-4 w-4 text-ink-3 group-hover:text-stamp" aria-hidden />
                  </div>
                  {x.where && <div className="truncate px-3 pt-2 text-xs text-ink-2">{x.where}</div>}
                  <div className="px-3 pb-3 pt-1">
                    <p className={cx("line-clamp-4 whitespace-pre-line text-sm leading-relaxed text-ink", x.mono && "font-mono text-xs")}>{x.body}</p>
                  </div>
                </button>
              </li>
            );
          })}
        </ol>
      )}
      {note && <p className="mt-3 text-xs text-ink-2">{note}</p>}
    </div>
  );
}
