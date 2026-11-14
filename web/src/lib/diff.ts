// Line diff for the approval preview (what a pending write or edit would change). Display only:
// the server applies the change, never the UI.

export type DiffLine = { kind: "same" | "add" | "del"; text: string };

export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before === "" ? [] : before.split("\n");
  const b = after === "" ? [] : after.split("\n");
  // LCS table; files shown here are small (the workspace caps reads and writes).
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i]![j] =
        a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i]! });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      out.push({ kind: "del", text: a[i++]! });
    } else {
      out.push({ kind: "add", text: b[j++]! });
    }
  }
  while (i < a.length) out.push({ kind: "del", text: a[i++]! });
  while (j < b.length) out.push({ kind: "add", text: b[j++]! });
  return out;
}

/** Keep changed lines plus `context` unchanged lines around them; long unchanged runs collapse. */
export function withContext(lines: DiffLine[], context = 3): (DiffLine | { kind: "gap"; count: number })[] {
  const keep = lines.map(() => false);
  lines.forEach((l, idx) => {
    if (l.kind !== "same") {
      for (let k = Math.max(0, idx - context); k <= Math.min(lines.length - 1, idx + context); k++)
        keep[k] = true;
    }
  });
  const out: (DiffLine | { kind: "gap"; count: number })[] = [];
  let gap = 0;
  lines.forEach((l, idx) => {
    if (keep[idx]) {
      if (gap) out.push({ kind: "gap", count: gap });
      gap = 0;
      out.push(l);
    } else gap++;
  });
  if (gap) out.push({ kind: "gap", count: gap });
  return out;
}

/** What workspace_edit will produce: the first occurrence of `old` replaced by `new`. Mirrors the
 * tool's contract only to preview it; null when `old` is not in the file (the tool will refuse). */
export function applyEdit(content: string, oldText: string, newText: string): string | null {
  const at = content.indexOf(oldText);
  if (at === -1) return null;
  return content.slice(0, at) + newText + content.slice(at + oldText.length);
}
