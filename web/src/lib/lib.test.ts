import { describe, expect, it } from "vitest";
import { applyEdit, lineDiff, withContext } from "./diff";
import { SseParser } from "./sse";
import { splitCitations } from "./format";

describe("SseParser", () => {
  it("parses events across chunk boundaries and skips keep-alive comments", () => {
    const p = new SseParser();
    expect(p.push('event: start\ndata: {"a":')).toEqual([]);
    expect(p.push('1}\n\n: keep-alive\n\nevent: final\r\ndata: {"b":2}\r\n\r\n')).toEqual([
      { event: "start", data: '{"a":1}' },
      { event: "final", data: '{"b":2}' },
    ]);
  });

  it("joins multi-line data and defaults the event name", () => {
    expect(new SseParser().push("data: one\ndata: two\n\n")).toEqual([
      { event: "message", data: "one\ntwo" },
    ]);
  });
});

describe("lineDiff", () => {
  it("marks added and removed lines", () => {
    expect(lineDiff("a\nb\nc", "a\nB\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "B" },
      { kind: "same", text: "c" },
    ]);
  });

  it("treats a new file as all additions and collapses long unchanged runs", () => {
    expect(lineDiff("", "x\ny").map((l) => l.kind)).toEqual(["add", "add"]);
    const before = Array.from({ length: 20 }, (_, i) => `l${i}`).join("\n");
    const after = before.replace("l10", "changed");
    const shown = withContext(lineDiff(before, after), 1);
    expect(shown[0]).toEqual({ kind: "gap", count: 9 });
    expect(shown.filter((l) => l.kind === "add")).toEqual([{ kind: "add", text: "changed" }]);
  });

  it("previews an edit only when the old text exists", () => {
    expect(applyEdit("def f():\n    pass\n", "pass", "return 1")).toBe("def f():\n    return 1\n");
    expect(applyEdit("abc", "zzz", "y")).toBeNull();
  });
});

describe("splitCitations", () => {
  it("splits [S1] and [T2] markers out of answer text", () => {
    expect(splitCitations("RPO is 5 minutes [S1], i.e. 300 s [T1].")).toEqual([
      "RPO is 5 minutes ",
      { label: "S1" },
      ", i.e. 300 s ",
      { label: "T1" },
      ".",
    ]);
  });
});

import { buildHands } from "../components/CustodyChain";
import type { LedgerEvent } from "./types";

const ev = (seq: number, kind: string, name: string, status = "ok", data: Record<string, unknown> = {}, at?: string): LedgerEvent => ({
  seq,
  kind,
  name,
  status,
  data,
  at,
});

describe("buildHands", () => {
  it("groups the ledger into signed hands and attaches the approval to the Approval hand", () => {
    const hands = buildHands([
      ev(0, "step", "classify_request"),
      ev(1, "model_call", "classify", "ok", { model: "qwen3:1.7b" }),
      ev(2, "step", "select_or_execute_tools"),
      ev(3, "model_call", "select_tools", "ok", { model: "qwen3:8b" }),
      ev(4, "approval", "workspace_write", "pending"),
      ev(5, "step", "human_approval"),
    ]);
    expect(hands.map((h) => h.step)).toEqual(["Classify", "Tools", "Approval"]);
    expect(hands[1]).toMatchObject({ signers: ["qwen3:8b"], state: "ok" });
    expect(hands[2]).toMatchObject({ signers: ["you"], state: "pending" });
  });

  it("records who countersigned and when, and adds the Approval hand if the run has not reached it", () => {
    const signed = buildHands([ev(0, "step", "select_or_execute_tools"), ev(1, "approval", "workspace_write", "ok", {}, "2026-11-14T10:00:00Z")]);
    expect(signed.at(-1)).toMatchObject({ step: "Approval", state: "ok", signedAt: "2026-11-14T10:00:00Z" });
    const rejected = buildHands([ev(0, "step", "human_approval"), ev(1, "approval", "workspace_edit", "rejected")]);
    expect(rejected[0]).toMatchObject({ state: "rejected", notes: ["rejected"] });
  });

  it("counts repeated signers and numbers loop turns", () => {
    const hands = buildHands([
      ev(0, "step", "step"),
      ev(1, "tool_call", "document_search"),
      ev(2, "tool_call", "document_search", "ok", { cache: "hit" }),
      ev(3, "step", "step"),
    ]);
    expect(hands.map((h) => h.step)).toEqual(["Turn 1", "Turn 2"]);
    expect(hands[0]).toMatchObject({ signers: ["document_search ×2"], notes: ["cache hit"] });
  });
});

import { cleanSnippet } from "../components/Evidence";

describe("cleanSnippet", () => {
  it("drops markdown heading markers and keeps list items on their own lines", () => {
    expect(cleanSnippet("## Availability and Recovery - Availability target: 99.95%. - Recovery Point Objective (RPO): 5 minutes")).toBe(
      "Availability and Recovery\n– Availability target: 99.95%.\n– Recovery Point Objective (RPO): 5 minutes",
    );
  });
});
