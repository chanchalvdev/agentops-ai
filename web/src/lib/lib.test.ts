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
