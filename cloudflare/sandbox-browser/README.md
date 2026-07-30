# Cloudflare Sandbox Browser Worker — Stage 1

`web.browser.isolated` の既存 typed action/wire 契約へ将来接続する、Cloudflare Sandbox 用の未接続 Worker scaffold です。Quick Actions / Browser Rendering とは別 module で、Discord command・runtime composition・capability catalog には登録していません。

- endpoint は既定OFF、request ごとに一意 sandbox ID、`enableInternet: false`、明示 `allowedHosts`、空 environment、ephemeral profile、finally destroy を型と offline test で固定します。Sandbox binding はまだ登録しません。
- outbound は HTTPS の exact host と `GET` / `HEAD` だけです。POST/PUT/PATCH/DELETE/CONNECT、任意 command、shell、host file、CDP、script evaluation、download/upload は公開 schema にありません。
- `destroy()` の Promise resolve を `destroy_completed` と返すだけで、Cloudflare が独立した destroy receipt や post-destroy liveness 証明を発行すると主張しません。

Sandbox SDK の desktop/browser automation は削除済みで、Sandbox 内 Chromium/Playwright の公式 API・image 手順は確認できませんでした。Dockerfile は version-pinned base image を要求する未build template に留め、Playwright install、browser launch、SDK binding、deploy は未接続です。`trustedOutbound` も後続のコード所有 adapter が接続すべき純粋な境界であり、現Stageで実Sandboxへ適用済みではありません。将来の adapter は `getSandbox(..., { transport: "rpc", enableDefaultSession: false, keepAlive: false })` を使い、SDK と一致する `docker.io/cloudflare/sandbox:<version>` を設定する必要があります。

この Stage は runtime ready、live success、containment 実証、interactive side-effect mode を意味しません。成功後 Forge 候補通知につなぐ receipt 以外の通知・DM・自動昇格は実装していません。
