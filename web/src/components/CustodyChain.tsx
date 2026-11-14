import { useCallback, useEffect, useRef, useState } from "react";
import { cx, label } from "./ui";
import { ms } from "../lib/format";
import type { LedgerEvent } from "../lib/types";

// The custody chain: a run's ledger read as signed handoffs. Each workflow step is one hand,
// signed by the models and tools that acted in it, with its time and tokens; the chain ends in
// a seal carrying the outcome. Built only from ledger events the API recorded.

export interface Hand {
  key: string;
  step: string;
  signers: string[];
  latencyMs: number;
  tokens: number;
  state: "ok" | "error" | "rejected" | "pending";
  notes: string[];
  signedAt?: string; // when a human signed this hand (approvals)
}

const STEP_NAMES: Record<string, string> = {
  classify_request: "Classify",
  plan_workflow: "Plan",
  retrieve_evidence: "Retrieve",
  select_or_execute_tools: "Tools",
  human_approval: "Approval",
  generate_answer: "Answer",
  validate_citations: "Cite check",
  verify_answer: "Verify",
  retry_or_revise: "Revise",
  finalize_execution: "Finalize",
};

function stepName(raw: string, index: number): string {
  if (raw === "step") return `Turn ${index}`; // the custom loop records one step per model turn
  return STEP_NAMES[raw] ?? label(raw);
}

export function buildHands(events: LedgerEvent[]): Hand[] {
  const hands: Hand[] = [];
  const counts = new Map<Hand, Map<string, number>>();
  let turns = 0;
  const open = (step: string, seq: number): Hand => {
    if (step === "step") turns += 1;
    const hand: Hand = { key: `${seq}`, step: stepName(step, turns), signers: [], latencyMs: 0, tokens: 0, state: "ok", notes: [] };
    hands.push(hand);
    counts.set(hand, new Map());
    return hand;
  };
  let current: Hand | null = null;
  // Approval events belong to the Approval hand (the human's countersignature), even though the
  // ledger records the request inside the tool-selection step that proposed the call.
  const approvals: LedgerEvent[] = [];
  for (const e of events) {
    if (e.kind === "final") continue;
    if (e.kind === "approval") {
      approvals.push(e);
      continue;
    }
    if (e.kind === "step") {
      current = open(e.name, e.seq);
      continue;
    }
    current ??= open("start", e.seq);
    const hand: Hand = current;
    const signer = e.kind === "model_call" ? String(e.data?.model ?? e.name) : e.name;
    if (e.kind === "model_call" || e.kind === "tool_call") {
      const c = counts.get(hand)!;
      c.set(signer, (c.get(signer) ?? 0) + 1);
    }
    hand.latencyMs += e.latency_ms ?? 0;
    hand.tokens += (e.input_tokens ?? 0) + (e.output_tokens ?? 0);
    if (e.kind === "tool_call" && e.data?.cache === "hit") hand.notes.push("cache hit");
    if (e.kind === "correction" && e.status === "ok") hand.notes.push(`corrected: ${label(e.name)}`);
    if (e.status === "pending") hand.state = "pending";
    else if (e.status === "rejected" && hand.state === "ok") {
      hand.state = "rejected";
      hand.notes.push(e.error_code ? label(e.error_code) : "refused");
    } else if (e.status === "error") {
      hand.state = "error";
      hand.notes.push(e.error_code ? label(e.error_code) : "error");
    }
  }
  if (approvals.length) {
    const approvalHand = [...hands].reverse().find((h) => h.step === "Approval") ?? open("human_approval", approvals[0]!.seq);
    approvalHand.notes = approvalHand.notes.filter((n) => n !== "refused");
    counts.get(approvalHand)!.set("you", 1);
    const last = approvals[approvals.length - 1]!;
    if (last.status === "pending") approvalHand.state = "pending";
    else if (last.status === "rejected") {
      approvalHand.state = "rejected";
      approvalHand.notes.push("rejected");
      approvalHand.signedAt = last.at;
    } else {
      approvalHand.state = "ok";
      approvalHand.signedAt = last.at;
    }
  }
  for (const hand of hands) {
    const c = counts.get(hand)!;
    hand.signers = [...c.entries()].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name));
  }
  return hands;
}

const SEAL: Record<string, { text: string; tone: string }> = {
  supported: { text: "Supported", tone: "border-stamp text-stamp" },
  answered: { text: "Answered", tone: "border-stamp text-stamp" },
  insufficient_evidence: { text: "No evidence", tone: "border-caution text-caution" },
  awaiting_approval: { text: "Held for you", tone: "border-caution text-caution" },
};

export function Seal({
  status,
  verification,
  totalMs,
}: {
  status: string;
  verification?: string | null;
  totalMs?: number | null;
}) {
  const key = status === "answered" && verification === "supported" ? "supported" : status;
  const seal = SEAL[key] ?? { text: label(status), tone: "border-seal text-seal" };
  return (
    <div
      className={cx(
        "flex shrink-0 -rotate-2 animate-stamp flex-col items-center justify-center border-2 px-3 py-1.5 text-center",
        seal.tone,
      )}
      role="status"
      aria-label={`Sealed: ${seal.text}`}
    >
      <span className="text-sm font-extrabold uppercase tracking-[0.08em]">{seal.text}</span>
      {totalMs != null && <span className="font-mono text-2xs">sealed {ms(totalMs)}</span>}
    </div>
  );
}

export function CustodyChain({
  events,
  live,
  final,
  focus,
  compact,
}: {
  events: LedgerEvent[];
  live?: boolean;
  final?: { status: string; verification?: string | null; totalMs?: number | null } | null;
  focus?: string | null;
  compact?: boolean;
}) {
  const hands = buildHands(events);
  const strip = useRef<HTMLOListElement>(null);
  const [edges, setEdges] = useState({ start: false, end: false });
  const measure = useCallback(() => {
    const el = strip.current;
    if (!el) return;
    setEdges({ start: el.scrollLeft > 2, end: el.scrollLeft + el.clientWidth < el.scrollWidth - 2 });
  }, []);
  const pending = hands.some((h) => h.state === "pending");
  useEffect(() => {
    // While a run is live, or a hand awaits you, the newest hand is the one that matters: keep the
    // strip's end in view. A sealed record opens at its start, read in order.
    const el = strip.current;
    if (el && (live || pending) && el.scrollWidth > el.clientWidth) el.scrollLeft = el.scrollWidth;
    measure();
  }, [hands.length, live, pending, measure]);
  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);
  // Overflow cue: fade whichever edge hides hands, so a clipped hand never reads as the whole chain.
  const mask =
    edges.start || edges.end
      ? `linear-gradient(to right, ${edges.start ? "transparent, black 40px" : "black"}, ${edges.end ? "black calc(100% - 40px), transparent" : "black"})`
      : undefined;
  return (
    <div className={cx("flex items-center bg-wash", compact ? "py-3" : "py-4")}>
    <ol
      ref={strip}
      onScroll={measure}
      style={mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined}
      className={cx("flex min-w-0 flex-1 items-stretch overflow-x-auto", compact ? "px-4" : "px-6")}
      aria-label="Custody chain: each step of the run, who signed it, and how long it took"
    >
      {hands.length === 0 && live && (
        <li className="flex items-center gap-2 text-sm text-ink-2">
          <span className="h-2 w-2 animate-live bg-stamp" aria-hidden />
          Opening the record…
        </li>
      )}
      {hands.map((h, i) => {
        const isLive = live && i === hands.length - 1;
        const dim = focus != null && focus !== h.key;
        return (
          <li
            key={h.key}
            className={cx(
              "min-w-[96px] shrink-0 grow basis-auto animate-sign border-l-1.5 px-3 transition-opacity duration-300 first:border-l-0 first:pl-0",
              h.state === "error" || h.state === "rejected" ? "border-seal" : "border-ink",
              dim && "opacity-40",
            )}
          >
            <div className="flex items-center gap-1.5 text-xs font-semibold text-ink-2">
              <span className="font-mono text-2xs text-ink-3">{String(i + 1).padStart(2, "0")}</span>
              {h.step}
              {isLive && <span className="h-1.5 w-1.5 animate-live bg-stamp" aria-label="in progress" />}
            </div>
            <div
              className={cx(
                "mt-1 font-mono text-sm font-bold leading-snug",
                h.state === "error" || h.state === "rejected" ? "text-seal" : h.state === "pending" ? "text-caution" : "text-ink",
                h.signedAt && "text-stamp",
              )}
            >
              {h.state === "pending" ? (
                "awaiting you"
              ) : h.signers.length ? (
                h.signers.map((s) => (
                  <div key={s} className="whitespace-nowrap">
                    {s === "you" && h.signedAt ? `you · ${new Date(h.signedAt).toLocaleTimeString()}` : s}
                  </div>
                ))
              ) : (
                "checked"
              )}
            </div>
            <div className="mt-0.5 whitespace-nowrap font-mono text-xs text-ink-2">{h.latencyMs > 0 ? ms(h.latencyMs) : "–"}</div>
            {h.tokens > 0 && <div className="whitespace-nowrap font-mono text-xs text-ink-2">{h.tokens.toLocaleString()} tok</div>}
            {h.notes.length > 0 && (
              <div className={cx("mt-0.5 truncate text-2xs", h.state === "ok" ? "text-stamp" : "text-seal")}>
                {h.notes.join(" · ")}
              </div>
            )}
          </li>
        );
      })}
    </ol>
      {final && (
        <div className="shrink-0 border-l border-rule pl-4 pr-6">
          <Seal status={final.status} verification={final.verification} totalMs={final.totalMs} />
        </div>
      )}
    </div>
  );
}
