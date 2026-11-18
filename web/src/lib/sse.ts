// Server-Sent Events over a POST response. EventSource only does GET, and the agent stream is a
// POST with a JSON body, so the stream is read with fetch and parsed here.

export interface SseMessage {
  event: string;
  data: string;
  /** The `id:` field, if the message had one (used as Last-Event-ID on reconnect). */
  id?: string;
}

/** Incremental parser: feed text chunks as they arrive, get complete messages back. Comment
 * lines (": keep-alive") are dropped; a message ends at a blank line. */
export class SseParser {
  private buffer = "";

  push(chunk: string): SseMessage[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");
    const out: SseMessage[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      let event = "message";
      let id: string | undefined;
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line === "" || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
        else if (field === "id") id = value;
      }
      if (data.length) out.push(id === undefined ? { event, data: data.join("\n") } : { event, data: data.join("\n"), id });
    }
    return out;
  }
}
