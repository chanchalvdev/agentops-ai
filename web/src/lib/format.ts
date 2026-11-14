export type Segment = string | { label: string };

/** Answer text with its [S1]/[T1] citation markers split out, so they can be rendered as links. */
export function splitCitations(text: string): Segment[] {
  const out: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(/\[([ST]\d+)\]/g)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    out.push({ label: m[1]! });
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function ms(value: number | null | undefined): string {
  if (value == null) return "–";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

export function pct(value: number | null | undefined, digits = 0): string {
  return value == null ? "–" : `${(value * 100).toFixed(digits)}%`;
}

export function num(value: number | null | undefined, digits = 0): string {
  return value == null ? "–" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "–";
  return new Date(iso).toLocaleString();
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
