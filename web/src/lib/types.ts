// Response shapes of the public API (see /docs). Only the fields the UI reads are typed.

export interface Citation {
  source_id: string;
  chunk_id: string;
  document_id: string;
  filename: string;
  section: string | null;
  page_start: number | null;
  page_end: number | null;
  snippet: string;
}

export interface ToolReference {
  source_id: string;
  tool: string;
  output: Record<string, unknown>;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
}

export interface LedgerEvent {
  seq: number;
  kind: string;
  name: string;
  status: string;
  error_code?: string | null;
  message?: string | null;
  latency_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  data: Record<string, unknown>;
  at?: string;
}

export type TraceEvent = LedgerEvent;

export interface AgentRunResult {
  execution_id: string;
  mode: "graph" | "loop";
  status: string;
  answer: string;
  citations: Citation[];
  tool_references: ToolReference[];
  error_code: string | null;
  verification: string | null;
  steps: number;
  model_calls: number;
  tool_calls: number;
  corrections: Record<string, number>;
  usage: Usage;
  cost_usd: string | null;
  total_ms: number;
}

export type StreamEvent =
  | { event: "start"; data: { execution_id: string; mode: string } }
  | { event: "citation"; data: Citation }
  | { event: "final"; data: AgentRunResult }
  | { event: "error"; data: { code: string; execution_id: string } }
  | { event: string; data: LedgerEvent };

export interface DocumentOut {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  version: number;
  is_latest: boolean;
  status: "pending" | "processing" | "ready" | "failed";
  error: string | null;
  page_count: number | null;
  chunk_count: number;
  chunking_config: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DocumentList {
  items: DocumentOut[];
  total: number;
}

export interface ChunkOut {
  id: string;
  document_id: string;
  filename: string;
  document_version: number;
  chunk_index: number;
  section: string | null;
  page_start: number | null;
  page_end: number | null;
  text: string;
}

export interface Execution {
  id: string;
  mode: string;
  status: string;
  question: string;
  answer: string | null;
  citations: Citation[];
  error_code: string | null;
  steps: number;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string | null;
  total_ms: number | null;
  created_at: string;
  finished_at: string | null;
}

/** What the Approvals screen shows once a decided run has resumed and finished. */
export interface Resumed {
  execution_id: string;
  status: string;
  answer: string;
  total_ms: number | null;
}

export interface Approval {
  id: string;
  execution_id: string;
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | "expired";
  reason: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface Entry {
  name: string;
  type: "file" | "dir" | "symlink" | "other";
  size: number | null;
}

export interface ListResult {
  path: string;
  entries: Entry[];
  truncated: boolean;
}

export interface FileContent {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
}

export interface SearchResult {
  query: string;
  matches: { path: string; line: number; text: string }[];
  files_scanned: number;
  truncated: boolean;
}

export interface WriteResult {
  outcome: string;
  path: string;
  bytes: number;
  created: boolean;
}

export interface ExecResult {
  argv: string[];
  exit_code: number | null;
  output: string;
  truncated: boolean;
  timed_out: boolean;
  duration_ms: number;
}

export interface AuditEntry {
  id: number;
  actor: string;
  operation: string;
  target: string;
  status: string;
  error_code: string | null;
  exit_code: number | null;
  duration_ms: number;
  created_at: string;
}

export interface RunRow {
  id: string;
  status: string;
  mode: string;
  total_cases: number;
  passed_cases: number;
  pass_rate: number | null;
  config: Record<string, unknown> & { profile?: string | null };
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface CaseReport {
  id: string;
  category: string;
  passed: boolean | null;
  status: string;
  execution_id: string | null;
  latency_ms: number | null;
  failures: string[];
  answer: string | null;
}

export interface RunReport {
  run_id: string;
  mode: string;
  config: Record<string, unknown>;
  aggregate: {
    total: number;
    passed: number;
    pass_rate: number | null;
    by_category: Record<string, { total: number; passed: number; pass_rate: number | null }>;
    by_evaluator: Record<string, { applicable: number; passed: number; pass_rate: number | null }>;
    latency_ms: { p50: number | null; p95: number | null };
    tokens_per_request?: number | null;
    quality_score?: number | null;
  };
  cases: CaseReport[];
}

export interface Comparison {
  base_run: string;
  head_run: string;
  pass_rate: Record<string, number | null>;
  newly_failing: string[];
  newly_passing: string[];
  breaches: string[];
}

export interface Signal {
  category: string;
  metric: string;
  status: "ok" | "drift" | "insufficient_data";
  evidence: "measured" | "suspected" | null;
  severity: "critical" | "warning" | "info" | null;
  baseline: number | null;
  current: number | null;
  threshold: string;
  samples: number | null;
  message: string;
}

export interface Alert {
  id: string;
  category: string;
  metric: string;
  severity: string;
  evidence: string;
  message: string;
  status: string;
  created_at: string;
}

export interface CheckOut {
  id: string;
  created_at: string;
  report: { status: string; signals: Signal[] };
  alerts: Alert[];
}

export interface MonitoringSummary {
  drift_status: string | null;
  last_check_at: string | null;
  open_alerts: Alert[];
  evaluation_history: {
    run_id: string;
    started_at: string;
    profile: string | null;
    pass_rate: number | null;
    total: number;
    partial: boolean;
    p95_latency_ms: number | null;
  }[];
  operational: {
    executions: number;
    success_rate: number | null;
    provider_error_rate: number | null;
    latency_ms: { p50: number | null; p95: number | null };
    tokens: { per_execution: number | null };
  };
}
