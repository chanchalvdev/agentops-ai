export type ScreenId = "chat" | "documents" | "workspace" | "approvals" | "executions" | "evaluations";

export function navigate(screen: ScreenId, arg?: string): void {
  location.hash = `#/${screen}${arg ? `/${encodeURIComponent(arg)}` : ""}`;
}
