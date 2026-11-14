import { AlertTriangle, Loader2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../lib/api";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "danger" | "ghost" | "seal";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-ink text-paper hover:bg-[#25304a] active:bg-[#0f1626]",
  secondary: "border-1.5 border-ink bg-paper text-ink hover:bg-wash",
  danger: "border-1.5 border-seal bg-paper text-seal hover:bg-seal-soft",
  ghost: "text-ink-2 hover:bg-wash hover:text-ink",
  seal: "bg-stamp text-white hover:bg-stamp-deep",
};

export function Button({
  children,
  onClick,
  variant = "secondary",
  size = "md",
  disabled,
  type = "button",
  title,
  busy,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: Variant;
  size?: "sm" | "md";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  busy?: boolean;
}) {
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap font-semibold transition-colors duration-150 ease-out",
        "disabled:cursor-not-allowed disabled:opacity-45",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-sm",
        VARIANTS[variant],
      )}
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
      {children}
    </button>
  );
}

/** A ruled section of a record: a heading on an ink rule, no box. */
export function Panel({
  title,
  meta,
  actions,
  children,
  className,
  flush,
}: {
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <section className={cx("min-w-0", className)}>
      <header className="flex min-h-9 items-end justify-between gap-3 border-b-1.5 border-ink pb-2">
        <div className="flex min-w-0 items-baseline gap-3">
          <h2 className="truncate text-lg font-bold">{title}</h2>
          {meta && <span className="truncate text-sm text-ink-2">{meta}</span>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </header>
      <div className={flush ? "" : "pt-4"}>{children}</div>
    </section>
  );
}

export type Tone = "verified" | "critical" | "pending" | "info" | "neutral" | "stamp";

const TONES: Record<Tone, { text: string; bg: string; swatch: string }> = {
  verified: { text: "text-record", bg: "bg-record-soft", swatch: "bg-record" },
  critical: { text: "text-seal", bg: "bg-seal-soft", swatch: "bg-seal" },
  pending: { text: "text-caution", bg: "bg-caution-soft", swatch: "bg-caution" },
  info: { text: "text-info", bg: "bg-info-soft", swatch: "bg-info" },
  neutral: { text: "text-ink-2", bg: "bg-wash", swatch: "bg-ink-3" },
  stamp: { text: "text-stamp", bg: "bg-stamp-soft", swatch: "bg-stamp" },
};

/** Tone for every status word the API returns. */
export function toneFor(status: string | null | undefined): Tone {
  switch (status) {
    case "answered":
    case "ready":
    case "completed":
    case "ok":
    case "supported":
    case "passed":
    case "verified":
      return "verified";
    case "approved":
      return "stamp";
    case "failed":
    case "failed_validation":
    case "rejected":
    case "error":
    case "drift":
    case "critical":
    case "timeout":
    case "budget_exhausted":
    case "cancelled":
    case "unsupported":
      return "critical";
    case "insufficient_evidence":
    case "awaiting_approval":
    case "pending":
    case "processing":
    case "running":
    case "suspected":
    case "warning":
      return "pending";
    case "insufficient_data":
    case "info":
      return "info";
    default:
      return "neutral";
  }
}

export const label = (s: string) => s.replace(/_/g, " ");

/** A status mark: a square swatch and the status word, never colour alone. */
export function Status({ value, tone, className }: { value: string; tone?: Tone; className?: string }) {
  const t = TONES[tone ?? toneFor(value)];
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 whitespace-nowrap px-1.5 py-px text-xs font-semibold",
        t.bg,
        t.text,
        className,
      )}
    >
      <span className={cx("h-1.5 w-1.5 shrink-0", t.swatch)} aria-hidden />
      {label(value)}
    </span>
  );
}

/** A record number: EX-9A49 for executions, AP- approvals, EV- evaluation runs. */
export function recordId(prefix: string, uuid: string): string {
  return `${prefix}-${uuid.replace(/-/g, "").slice(0, 4).toUpperCase()}`;
}

export function RecordId({ prefix, id, invert }: { prefix: string; id: string; invert?: boolean }) {
  return (
    <span
      title={id}
      className={cx(
        "inline-block whitespace-nowrap px-1.5 py-px font-mono text-xs font-bold",
        invert ? "bg-ink text-paper" : "border border-ink/30 text-ink",
      )}
    >
      {recordId(prefix, id)}
    </span>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cx("h-4 w-4 animate-spin text-ink-2", className)} aria-label="Loading" />;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : null;
  const message = e?.message ?? (error instanceof Error ? error.message : String(error));
  let hint = "";
  if (e?.status === 401) hint = "Set a valid API key in the rail.";
  else if (e?.status === 403) hint = "This API key's tenant is not allowed to do this.";
  else if (e?.retryAfter) hint = `Rate limited: try again in ${e.retryAfter} s.`;
  else if (e?.status === 0 || message === "Failed to fetch") hint = "The API is unreachable. Is it running?";
  return (
    <div role="alert" className="flex gap-3 border border-seal/35 bg-seal-soft px-4 py-3 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-seal" aria-hidden />
      <div className="min-w-0">
        <p className="font-semibold text-seal">
          {message}
          {e && <span className="ml-2 font-mono text-xs font-normal">{e.status} {e.code}</span>}
        </p>
        {hint && <p className="mt-0.5 text-ink-2">{hint}</p>}
        {e?.requestId && <p className="mt-0.5 font-mono text-2xs text-ink-3">request {e.requestId}</p>}
      </div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="border-1.5 border-dashed border-rule px-6 py-10 text-center">
      <p className="text-md font-semibold">{title}</p>
      {children && <div className="mx-auto mt-1 max-w-md text-sm text-ink-2">{children}</div>}
    </div>
  );
}

export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <pre
      className={cx(
        "overflow-auto whitespace-pre-wrap break-words border border-rule bg-paper p-3 font-mono text-xs leading-relaxed text-ink",
        className,
      )}
    >
      {children}
    </pre>
  );
}

/** A measured figure: the number in tabular mono, its unit and meaning beneath. */
export function Figure({ value, label: what, hint }: { value: ReactNode; label: string; hint?: string }) {
  return (
    <div className="min-w-0" title={hint}>
      <div className="font-mono text-xl font-medium leading-none">{value}</div>
      <div className="mt-1.5 text-xs text-ink-2">{what}</div>
    </div>
  );
}

/** Load data from the API with loading/error state; `reload` re-runs the loader. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const current = useRef(0);
  const reload = useCallback(async () => {
    const id = ++current.current;
    setLoading(true);
    try {
      const value = await loader();
      if (id === current.current) {
        setData(value);
        setError(null);
      }
    } catch (e) {
      if (id === current.current) setError(e);
    } finally {
      if (id === current.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

/** A side sheet for reading a record without leaving the page. */
export function Sheet({
  open,
  onClose,
  title,
  meta,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  meta?: ReactNode;
  children: ReactNode;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink/20" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        className="flex h-full w-full max-w-[560px] animate-sheet flex-col border-l-1.5 border-ink bg-ground shadow-sheet"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4 border-b-1.5 border-ink px-6 py-4">
          <div className="min-w-0">
            <h2 className="text-lg font-bold">{title}</h2>
            {meta && <p className="mt-0.5 text-sm text-ink-2">{meta}</p>}
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="-mr-2 grid h-8 w-8 shrink-0 place-items-center text-ink-2 hover:bg-wash hover:text-ink"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>
      </aside>
    </div>
  );
}

/** A page heading: the screen's name and one line on what it is for. */
export function PageHead({ title, sub, actions }: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-3xl font-extrabold">{title}</h1>
        {sub && <p className="mt-1 max-w-2xl text-md text-ink-2">{sub}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}

export const inputClass =
  "h-9 w-full border-1.5 border-ink/40 bg-paper px-3 text-sm text-ink transition-colors hover:border-ink/70 focus:border-stamp focus:outline-none";

export const selectClass =
  "h-9 border-1.5 border-ink/40 bg-paper pl-2.5 pr-8 text-sm text-ink hover:border-ink/70 focus:border-stamp focus:outline-none";
