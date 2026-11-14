// Thin client for the public REST API. No business rules live here: every decision (permissions,
// approvals, limits, tenant isolation) is the server's; the UI only sends the user's API key.
import { SseParser } from "./sse";
import type {
  AgentRunResult,
  Approval,
  AuditEntry,
  CheckOut,
  ChunkOut,
  Comparison,
  DocumentList,
  DocumentOut,
  ExecResult,
  Execution,
  FileContent,
  ListResult,
  MonitoringSummary,
  RunReport,
  RunRow,
  SearchResult,
  StreamEvent,
  TraceEvent,
  WriteResult,
} from "./types";

const KEY_STORAGE = "agentops.apiKey";

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* private mode: the key lives for this page only */
  }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
    readonly retryAfter?: number,
  ) {
    super(message);
  }
}

async function toError(res: Response): Promise<ApiError> {
  const retry = Number(res.headers.get("Retry-After")) || undefined;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string; request_id?: string } };
    const e = body.error ?? {};
    return new ApiError(res.status, e.code ?? "http_error", e.message ?? res.statusText, e.request_id, retry);
  } catch {
    return new ApiError(res.status, "http_error", res.statusText || `HTTP ${res.status}`, undefined, retry);
  }
}

function headers(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  const key = getApiKey();
  if (key) h.set("X-API-Key", key);
  return h;
}

async function request<T>(method: string, path: string, body?: unknown, query?: Record<string, unknown>): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  const url = qs.size ? `${path}?${qs}` : path;
  const init: RequestInit = { method, headers: headers() };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Headers).set("Content-Type", "application/json");
  }
  const res = await fetch(url, init);
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(path: string, query?: Record<string, unknown>) => request<T>("GET", path, undefined, query);

export const api = {
  health: () => get<{ status: string }>("/health"),
  ready: () => get<{ status: string; checks: Record<string, unknown> }>("/ready"),
  whoami: () => get<{ tenant_id: string; executions: number }>("/api/v1/metrics", { hours: 24 }),

  // documents
  documents: (status?: string) => get<DocumentList>("/api/v1/documents", { limit: 100, status }),
  upload: (file: File, metadata?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (metadata?.trim()) form.append("metadata", metadata.trim());
    return request<{ document: DocumentOut; duplicate: boolean }>("POST", "/api/v1/documents", form);
  },
  deleteDocument: (id: string) => request<void>("DELETE", `/api/v1/documents/${id}`),
  reindex: (id: string) => request<DocumentOut>("POST", `/api/v1/documents/${id}/reindex`),
  chunk: (documentId: string, chunkId: string) =>
    get<ChunkOut>(`/api/v1/documents/${documentId}/chunks/${chunkId}`),

  // agent
  executions: (limit = 50) => get<Execution[]>("/api/v1/executions", { limit }),
  execution: (id: string) => get<Execution>(`/api/v1/executions/${id}`),
  trace: (id: string) => get<TraceEvent[]>(`/api/v1/executions/${id}/trace`),
  approvals: (status: "pending" | "all") => get<Approval[]>("/api/v1/approvals", { status }),
  decide: (id: string, approve: boolean, reason?: string) =>
    request<AgentRunResult>("POST", `/api/v1/approvals/${id}/${approve ? "approve" : "reject"}`, {
      reason: reason || null,
    }),

  // workspace
  tree: (path: string) => get<ListResult>("/api/v1/workspace/tree", { path }),
  readFile: (path: string) => get<FileContent>("/api/v1/workspace/file", { path }),
  writeFile: (path: string, content: string) => request<WriteResult>("PUT", "/api/v1/workspace/file", { path, content }),
  searchWorkspace: (q: string) => get<SearchResult>("/api/v1/workspace/search", { q }),
  exec: (line: string) => request<ExecResult>("POST", "/api/v1/workspace/exec", { line }),
  audit: (limit = 100) => get<AuditEntry[]>("/api/v1/workspace/audit", { limit }),

  // evaluations & monitoring (operator tenants only; others get 403)
  evalRuns: () => get<RunRow[]>("/api/v1/evaluations/runs", { limit: 50 }),
  evalRun: (id: string) => get<RunReport>(`/api/v1/evaluations/runs/${id}`),
  compare: (base: string, head: string) => get<Comparison>("/api/v1/evaluations/compare", { base, head }),
  monitoringSummary: () => get<MonitoringSummary>("/api/v1/monitoring/summary"),
  latestDrift: () => get<CheckOut | null>("/api/v1/monitoring/drift"),
  runDriftCheck: () => request<CheckOut>("POST", "/api/v1/monitoring/check"),
};

/** POST /agent/run/stream, delivering each SSE event as it arrives. Resolves when the stream ends. */
export async function streamAgent(
  question: string,
  mode: "graph" | "loop",
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/v1/agent/run/stream", {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ question, mode }),
    signal,
  });
  if (!res.ok || !res.body) throw await toError(res);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const m of parser.push(value)) {
      let data: unknown = m.data;
      try {
        data = JSON.parse(m.data);
      } catch {
        /* keep raw text */
      }
      onEvent({ event: m.event, data } as StreamEvent);
    }
  }
}

/** The workspace terminal WebSocket: same executor and policy as the REST exec endpoint. */
export function openTerminal(): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/v1/workspace/terminal`);
  // Browsers cannot set headers on a WebSocket, so the key goes in the first frame (protocol).
  ws.addEventListener("open", () => ws.send(JSON.stringify({ api_key: getApiKey() })));
  return ws;
}
