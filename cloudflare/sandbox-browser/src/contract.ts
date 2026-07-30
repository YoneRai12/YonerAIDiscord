export const SANDBOX_BROWSER_PATH = "/v1/browser-sessions";
export const MAX_REQUEST_BYTES = 128 * 1024;
export const MAX_ACTIONS = 32;
export const MAX_ALLOWED_HOSTS = 8;
export const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
export const DEFAULT_TIMEOUT_MS = 30_000;
export const MAX_TIMEOUT_MS = 60_000;

export type BrowserAction =
  | { readonly type: "navigate"; readonly url: string }
  | { readonly type: "click"; readonly selector: string }
  | { readonly type: "type_text"; readonly selector: string; readonly text: string; readonly clear_first: boolean }
  | { readonly type: "select_option"; readonly selector: string; readonly value: string }
  | { readonly type: "scroll"; readonly delta_x: number; readonly delta_y: number }
  | { readonly type: "wait"; readonly milliseconds: number }
  | { readonly type: "screenshot"; readonly full_page: boolean; readonly image_format: "png" | "jpeg" }
  | { readonly type: "extract_text"; readonly selector: string | null };

export interface SandboxBrowserRequest {
  readonly request_id: string;
  readonly actions: readonly BrowserAction[];
  readonly allowed_hosts: readonly string[];
  readonly timeout_ms: number;
}

export interface SandboxBrowserOutput {
  readonly step_index: number;
  readonly kind: "screenshot" | "text";
  readonly media_type: "image/png" | "image/jpeg" | "text/plain; charset=utf-8";
  readonly data_base64: string;
}

export interface SandboxBrowserResult { readonly outputs: readonly SandboxBrowserOutput[]; }

export interface SandboxBootstrap {
  readonly session_id: string;
  readonly enableInternet: false;
  readonly allowedHosts: readonly string[];
  readonly ephemeralProfile: true;
  readonly environment: Readonly<Record<string, never>>;
}

/** Future adapter seam; it intentionally exposes no exec, filesystem, or CDP method. */
export interface SandboxSession {
  runBrowserActions(actions: readonly BrowserAction[], timeoutMs: number): Promise<SandboxBrowserResult>;
  destroy(): Promise<void>;
}

/** Must later use getSandbox(..., { transport: "rpc", enableDefaultSession: false, keepAlive: false }). */
export interface SandboxFactory { open(bootstrap: SandboxBootstrap): Promise<SandboxSession>; }

export interface SandboxWorkerEnv {
  readonly SANDBOX_BROWSER_ENABLED?: string;
  readonly sandboxFactory?: SandboxFactory;
}

/** Promise-resolve evidence only; this is not a Cloudflare-issued receipt. */
export interface SandboxDestroyReceipt { readonly destroy_attempted: true; readonly destroy_completed: true; }
