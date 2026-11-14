import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../lib/api";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
}) {
  const styles = {
    default:
      "border border-slate-300 bg-white hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800",
    primary: "bg-indigo-600 text-white hover:bg-indigo-500",
    danger: "bg-rose-600 text-white hover:bg-rose-500",
    ghost: "hover:bg-slate-200/60 dark:hover:bg-slate-800",
  }[variant];
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        styles,
      )}
    >
      {children}
    </button>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-2.5 dark:border-slate-800">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const TONES: Record<string, string> = {
  green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  red: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
  amber: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  blue: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300",
  gray: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

/** Colour for the statuses the API returns (runs, documents, approvals, signals, audit). */
export function toneFor(status: string | null | undefined): keyof typeof TONES {
  switch (status) {
    case "answered":
    case "ready":
    case "approved":
    case "completed":
    case "ok":
    case "supported":
      return "green";
    case "failed":
    case "failed_validation":
    case "rejected":
    case "error":
    case "drift":
    case "critical":
    case "timeout":
    case "budget_exceeded":
    case "cancelled":
      return "red";
    case "insufficient_evidence":
    case "awaiting_approval":
    case "pending":
    case "processing":
    case "running":
    case "suspected":
    case "warning":
    case "unsupported":
      return "amber";
    case "insufficient_data":
    case "info":
      return "blue";
    default:
      return "gray";
  }
}

export function Badge({ children, tone }: { children: ReactNode; tone?: keyof typeof TONES }) {
  const t = tone ?? toneFor(typeof children === "string" ? children : undefined);
  return <span className={cx("inline-block rounded px-1.5 py-0.5 text-xs font-medium", TONES[t])}>{children}</span>;
}

export function Spinner() {
  return <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : null;
  const message = e?.message ?? (error instanceof Error ? error.message : String(error));
  return (
    <div className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300">
      {e && <span className="mr-2 font-mono text-xs">{e.status} {e.code}</span>}
      {message}
      {e?.status === 401 && " — set a valid API key in the sidebar."}
      {e?.status === 403 && e.code === "permission_denied" && " — this API key's tenant is not allowed here."}
      {e?.retryAfter ? ` Retry in ${e.retryAfter} s.` : ""}
      {e?.requestId && <span className="ml-2 font-mono text-xs opacity-70">request {e.requestId}</span>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <pre className={cx("overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-100 p-3 font-mono text-xs dark:bg-slate-950", className)}>{children}</pre>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-md border border-slate-200 px-3 py-2 dark:border-slate-800" title={hint}>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
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

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <aside
        className="h-full w-full max-w-xl overflow-y-auto border-l border-slate-200 bg-white p-5 shadow-xl dark:border-slate-800 dark:bg-slate-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <h2 className="text-base font-semibold">{title}</h2>
          <Button variant="ghost" onClick={onClose}>Close</Button>
        </div>
        {children}
      </aside>
    </div>
  );
}
