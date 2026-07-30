import {
  MAX_ACTIONS, MAX_ALLOWED_HOSTS, MAX_OUTPUT_BYTES, MAX_REQUEST_BYTES, MAX_TIMEOUT_MS, SANDBOX_BROWSER_PATH,
  type BrowserAction, type SandboxBootstrap, type SandboxBrowserRequest, type SandboxBrowserResult,
  type SandboxDestroyReceipt, type SandboxFactory, type SandboxSession, type SandboxWorkerEnv,
} from "./contract.js";

const IDENTIFIER = /^[a-z0-9][a-z0-9._-]{0,119}$/;
const HOST = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;

export interface FetchLike { (input: Request): Promise<Response>; }

export default { fetch(request: Request, env: SandboxWorkerEnv): Promise<Response> { return handleSandboxBrowserRequest(request, env); } };

export async function handleSandboxBrowserRequest(request: Request, env: SandboxWorkerEnv): Promise<Response> {
  if (request.method !== "POST" || new URL(request.url).pathname !== SANDBOX_BROWSER_PATH) return response("not_found", 404);
  if (env.SANDBOX_BROWSER_ENABLED !== "true" || env.sandboxFactory === undefined) return response("unavailable", 503);
  let payload: SandboxBrowserRequest;
  try { payload = await decodeRequest(request); } catch { return response("invalid_request", 400); }
  const bootstrap: SandboxBootstrap = {
    session_id: `sandbox-${crypto.randomUUID()}`, enableInternet: false, allowedHosts: payload.allowed_hosts,
    ephemeralProfile: true, environment: {},
  };
  return runOneSandbox(env.sandboxFactory, bootstrap, payload);
}

export async function runOneSandbox(factory: SandboxFactory, bootstrap: SandboxBootstrap, request: SandboxBrowserRequest): Promise<Response> {
  let session: SandboxSession | undefined;
  let result: SandboxBrowserResult | undefined;
  let failed = false;
  try {
    session = await factory.open(bootstrap);
    result = await timeout(session.runBrowserActions(request.actions, request.timeout_ms), request.timeout_ms);
    validateResult(result);
  } catch { failed = true; }
  let receipt: SandboxDestroyReceipt | undefined;
  if (session !== undefined) {
    try { await session.destroy(); receipt = { destroy_attempted: true, destroy_completed: true }; } catch { failed = true; }
  } else failed = true;
  if (failed || result === undefined || receipt === undefined) return response("unavailable", 503);
  return json({ outputs: result.outputs, cleanup: receipt });
}

/** Last egress boundary: exact HTTPS host and GET/HEAD only; never logs URL or body. */
export async function trustedOutbound(request: Request, allowedHosts: readonly string[], fetcher: FetchLike = (input) => fetch(input)): Promise<Response> {
  let url: URL;
  try { url = new URL(request.url); } catch { return response("forbidden", 403); }
  if (url.protocol !== "https:" || url.username || url.password || !allowedHosts.includes(url.hostname)
    || (request.method !== "GET" && request.method !== "HEAD") || request.body !== null) return response("forbidden", 403);
  const outbound = new Request(request, { redirect: "manual" });
  const outboundResponse = await fetcher(outbound);
  if (outboundResponse.status >= 300 && outboundResponse.status < 400) return response("forbidden", 403);
  return outboundResponse;
}

async function decodeRequest(request: Request): Promise<SandboxBrowserRequest> {
  if (request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase() !== "application/json") throw new TypeError();
  const raw = await request.arrayBuffer();
  if (raw.byteLength > MAX_REQUEST_BYTES) throw new TypeError();
  const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
  if (!record(value) || !exactKeys(value, ["actions", "allowed_hosts", "request_id", "timeout_ms"])) throw new TypeError();
  if (typeof value.request_id !== "string" || !IDENTIFIER.test(value.request_id)) throw new TypeError();
  if (!Array.isArray(value.allowed_hosts) || value.allowed_hosts.length < 1 || value.allowed_hosts.length > MAX_ALLOWED_HOSTS) throw new TypeError();
  const hosts = value.allowed_hosts.map(host);
  if (new Set(hosts).size !== hosts.length || !integer(value.timeout_ms, 1, MAX_TIMEOUT_MS)) throw new TypeError();
  if (!Array.isArray(value.actions) || value.actions.length < 1 || value.actions.length > MAX_ACTIONS) throw new TypeError();
  return { request_id: value.request_id, actions: value.actions.map((item) => action(item, hosts)), allowed_hosts: hosts, timeout_ms: value.timeout_ms };
}

function action(value: unknown, hosts: readonly string[]): BrowserAction {
  if (!record(value) || typeof value.type !== "string") throw new TypeError();
  if (value.type === "navigate") {
    if (!exactKeys(value, ["type", "url"]) || typeof value.url !== "string") throw new TypeError();
    const url = new URL(value.url);
    if (url.protocol !== "https:" || url.username || url.password || !hosts.includes(url.hostname)) throw new TypeError();
    return { type: "navigate", url: value.url };
  }
  if (value.type === "click") {
    if (!exactKeys(value, ["selector", "type"]) || !shortString(value.selector, 512)) throw new TypeError();
    return { type: "click", selector: value.selector };
  }
  if (value.type === "type_text") {
    if (!exactKeys(value, ["clear_first", "selector", "text", "type"]) || !shortString(value.selector, 512) || typeof value.text !== "string" || value.text.length > 8_000 || typeof value.clear_first !== "boolean") throw new TypeError();
    return { type: "type_text", selector: value.selector, text: value.text, clear_first: value.clear_first };
  }
  if (value.type === "select_option") {
    if (!exactKeys(value, ["selector", "type", "value"]) || !shortString(value.selector, 512) || !shortString(value.value, 1_000)) throw new TypeError();
    return { type: "select_option", selector: value.selector, value: value.value };
  }
  if (value.type === "scroll") {
    if (!exactKeys(value, ["delta_x", "delta_y", "type"]) || !integer(value.delta_x, -20_000, 20_000) || !integer(value.delta_y, -20_000, 20_000) || (value.delta_x === 0 && value.delta_y === 0)) throw new TypeError();
    return { type: "scroll", delta_x: value.delta_x, delta_y: value.delta_y };
  }
  if (value.type === "wait") {
    if (!exactKeys(value, ["milliseconds", "type"]) || !integer(value.milliseconds, 1, 30_000)) throw new TypeError();
    return { type: "wait", milliseconds: value.milliseconds };
  }
  if (value.type === "screenshot") {
    if (!exactKeys(value, ["full_page", "image_format", "type"]) || typeof value.full_page !== "boolean" || (value.image_format !== "png" && value.image_format !== "jpeg")) throw new TypeError();
    return { type: "screenshot", full_page: value.full_page, image_format: value.image_format };
  }
  if (value.type === "extract_text") {
    if (!exactKeys(value, ["selector", "type"]) || (value.selector !== null && !shortString(value.selector, 512))) throw new TypeError();
    return { type: "extract_text", selector: value.selector };
  }
  throw new TypeError();
}

function validateResult(result: SandboxBrowserResult): void {
  if (!record(result) || !Array.isArray(result.outputs) || result.outputs.length > MAX_ACTIONS) throw new TypeError();
  let total = 0;
  for (const item of result.outputs) {
    if (!record(item) || !integer(item.step_index, 0, MAX_ACTIONS - 1) || (item.kind !== "screenshot" && item.kind !== "text") || typeof item.data_base64 !== "string" || item.data_base64.length > MAX_OUTPUT_BYTES * 2 || !base64(item.data_base64)) throw new TypeError();
    if (item.kind === "screenshot" && item.media_type !== "image/png" && item.media_type !== "image/jpeg") throw new TypeError();
    if (item.kind === "text" && item.media_type !== "text/plain; charset=utf-8") throw new TypeError();
    total += decodedBase64Bytes(item.data_base64);
    if (total > MAX_OUTPUT_BYTES) throw new TypeError();
  }
}

function host(value: unknown): string { if (typeof value !== "string" || value !== value.toLowerCase() || !HOST.test(value) || value === "localhost") throw new TypeError(); return value; }
function shortString(value: unknown, maximum: number): value is string { return typeof value === "string" && value.length >= 1 && value.length <= maximum; }
function integer(value: unknown, minimum: number, maximum: number): value is number { return typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum; }
function record(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean { const actual = Object.keys(value).sort(); const expected = [...keys].sort(); return actual.length === expected.length && actual.every((item, index) => item === expected[index]); }
function base64(value: string): boolean { return value.length % 4 === 0 && /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value); }
function decodedBase64Bytes(value: string): number { return (value.length / 4) * 3 - (value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0); }
function timeout<T>(promise: Promise<T>, milliseconds: number): Promise<T> { let timer: ReturnType<typeof setTimeout> | undefined; const timerPromise = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), milliseconds); }); return Promise.race([promise, timerPromise]).finally(() => { if (timer !== undefined) clearTimeout(timer); }); }
function response(code: string, status: number): Response { return json({ error: code }, status); }
function json(value: unknown, status = 200): Response { return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" } }); }
