/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API origin when the UI is hosted apart from it, e.g. https://agentops.example.io */
  readonly VITE_API_BASE_URL?: string;
  /** Public, rate-limited demo key prefilled for showcase builds. */
  readonly VITE_DEMO_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
