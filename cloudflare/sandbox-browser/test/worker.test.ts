import { handleSandboxBrowserRequest, runOneSandbox, trustedOutbound } from "../src/worker.js";
import { MAX_OUTPUT_BYTES, type BrowserAction, type SandboxBootstrap, type SandboxBrowserResult, type SandboxFactory, type SandboxSession, type SandboxWorkerEnv } from "../src/contract.js";

function equal(actual: unknown, expected: unknown): void { if (actual !== expected) throw new Error(`expected ${String(expected)}`); }
function truthy(value: unknown): void { if (!value) throw new Error("expected truthy"); }

class FakeSession implements SandboxSession {
  destroyCalls = 0;
  failDestroy = false;
  failRun = false;
  invalidOutput = false;
  oversizedOutput = false;
  async runBrowserActions(_actions: readonly BrowserAction[], _timeoutMs: number): Promise<SandboxBrowserResult> {
    if (this.failRun) throw new Error("private failure text");
    const mediaType: unknown = this.invalidOutput ? "application/octet-stream" : "text/plain; charset=utf-8";
    const data = this.oversizedOutput ? "AAAA".repeat(Math.ceil((MAX_OUTPUT_BYTES + 1) / 3)) : "b2s=";
    return { outputs: [{ step_index: 0, kind: "text", media_type: mediaType as "text/plain; charset=utf-8", data_base64: data }] };
  }
  async destroy(): Promise<void> { this.destroyCalls += 1; if (this.failDestroy) throw new Error("destroy failure"); }
}
class FakeFactory implements SandboxFactory {
  opens = 0;
  bootstraps: SandboxBootstrap[] = [];
  readonly session = new FakeSession();
  async open(bootstrap: SandboxBootstrap): Promise<SandboxSession> { this.opens += 1; this.bootstraps.push(bootstrap); return this.session; }
}
function request(body: object): Request { return new Request("https://worker.example/v1/browser-sessions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); }
function body(): object { return { request_id: "request-1", allowed_hosts: ["example.com"], timeout_ms: 100, actions: [{ type: "navigate", url: "https://example.com/path" }, { type: "extract_text", selector: "main" }] }; }

const disabled = await handleSandboxBrowserRequest(request(body()), {});
equal(disabled.status, 503);
const factory = new FakeFactory();
const env: SandboxWorkerEnv = { SANDBOX_BROWSER_ENABLED: "true", sandboxFactory: factory };
const invalid = await handleSandboxBrowserRequest(request({ ...body(), actions: [{ type: "exec", command: "whoami" }] }), env);
equal(invalid.status, 400);
equal(factory.opens, 0);

const success = await handleSandboxBrowserRequest(request(body()), env);
equal(success.status, 200);
equal(factory.opens, 1);
equal(factory.session.destroyCalls, 1);
equal(factory.bootstraps[0]?.enableInternet, false);
equal(Object.keys(factory.bootstraps[0]?.environment ?? {}).length, 0);
truthy((await success.text()).includes("destroy_completed"));

let outboundCalls = 0;
const fetcher = async (_request: Request): Promise<Response> => { outboundCalls += 1; return new Response("ok"); };
equal((await trustedOutbound(new Request("https://example.com/"), ["example.com"], fetcher)).status, 200);
equal((await trustedOutbound(new Request("https://example.com/", { method: "POST", body: "x" }), ["example.com"], fetcher)).status, 403);
equal((await trustedOutbound(new Request("http://example.com/"), ["example.com"], fetcher)).status, 403);
equal(outboundCalls, 1);
equal((await trustedOutbound(new Request("https://example.com/redirect"), ["example.com"], async () => new Response(null, { status: 302, headers: { location: "https://untrusted.example/" } }))).status, 403);

const failedFactory = new FakeFactory();
failedFactory.session.failDestroy = true;
const failed = await handleSandboxBrowserRequest(request(body()), { SANDBOX_BROWSER_ENABLED: "true", sandboxFactory: failedFactory });
equal(failed.status, 503);
equal((await failed.text()).includes("example.com"), false);

const runFailure = new FakeFactory();
runFailure.session.failRun = true;
const direct = await runOneSandbox(runFailure, { session_id: "sandbox-1", enableInternet: false, allowedHosts: ["example.com"], ephemeralProfile: true, environment: {} }, { request_id: "request-1", allowed_hosts: ["example.com"], timeout_ms: 100, actions: [{ type: "navigate", url: "https://example.com/" }] });
equal(direct.status, 503);
equal(runFailure.session.destroyCalls, 1);

const invalidOutput = new FakeFactory();
invalidOutput.session.invalidOutput = true;
const invalidResult = await handleSandboxBrowserRequest(request(body()), { SANDBOX_BROWSER_ENABLED: "true", sandboxFactory: invalidOutput });
equal(invalidResult.status, 503);
equal(invalidOutput.session.destroyCalls, 1);

const oversizedOutput = new FakeFactory();
oversizedOutput.session.oversizedOutput = true;
const oversizedResult = await handleSandboxBrowserRequest(request(body()), { SANDBOX_BROWSER_ENABLED: "true", sandboxFactory: oversizedOutput });
equal(oversizedResult.status, 503);
equal(oversizedOutput.session.destroyCalls, 1);

console.log("sandbox browser worker tests passed");
