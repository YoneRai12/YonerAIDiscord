# Private Search Sandbox scaffold

## 現在値

これは、YonerAI Search Gateway と SearXNG を別processにし、private/local-onlyで
組み立てるためのoffline scaffoldです。2026-07-29時点でimage build、container起動、
実検索、BOT composition、live readinessは未検証です。Docker/Podman/Hyper-Vの
installや起動もこのsliceでは行っていません。

`gateway`だけをhost loopback `127.0.0.1:8787`へ公開し、`searxng`にはhost portを
与えません。`gateway`は外部egressを持たないinternal networkだけに接続し、
`searxng`だけが検索engine向けegress networkにも接続します。永続volume、bind
mount、host secret、`.env`、Discord IDは渡しません。設定はDockerfileでimageへ
COPYされ、runtime書込みはtmpfsだけです。

## 固定image

- 監査用tag: `searxng/searxng:2026.7.26-b060c780d`
- multi-platform index digest:
  `sha256:d0aaeb14880e6e92bde1518fcc7261e995783367d63d95203383607bef9c6516`
- `linux/amd64` manifest digest:
  `sha256:fa1b0523e5a66c374fc04f4471f7ab54a718f33f864e8409e0db0133041eab3a`

Dockerfileはtagや`latest`ではなくindex digestを使い、Composeは`linux/amd64`を
固定します。上記は2026-07-29にDocker Hubの公式SearXNG repositoryで確認した
pin候補であり、「最新版」や実環境readinessの主張ではありません。

Gatewayも同じdigest固定imageをPython runtimeとして使い、code-owned
`yonerai_discord.search_fabric.server`をCOPY/importします。server moduleまたは
依存関係が不足する場合はDocker build自体が失敗します。別実装へ暗黙fallback
しません。build後のlocal image digestは、実composition前に別途記録が必要です。

## SearXNG設定

- `search.formats`は`json`だけです。公式Search APIは、未許可formatを403にします。
- engineは`duckduckgo`と`wikipedia`だけを`keep_only`で残します。
- safe searchはstrict、autocomplete/favicon proxy/public instanceは無効です。
- GatewayからSearXNGへは固定`POST /search` form requestを使う前提です。
- queryをURLや通常logへ入れず、永続volumeも持たないため、raw queryのtemplate上の
  retentionは0です。process memoryや外部engineへの送信まで「保存されない」と
  証明するものではありません。
- official entrypointがtmpfs上へ設定をcopyするとき、`ultrasecretkey` placeholderを
  毎container lifetimeのrandom値へ置換します。host credentialではなく、停止時に
  消える内部値です。

SearXNG limiterは意図的に`false`です。公式limiterはValkeyと正しい
`X-Forwarded-For`/`X-Real-IP`設定を必須とします。このlocal-only topologyでは
SearXNGを直接公開せず、Gateway側のbounded contract/resource limitを境界にします。
Valkeyを固定digestで追加しないままlimiterを有効扱いにはしません。public ingressへ
変える場合、このtemplateは使用不可です。

## 起動前blocker

次を満たすまではfail-closedで、readyとは扱いません。

1. code-owned `yonerai_discord.search_fabric.server`のexact CLIと
   `yonerai.search-request.v1`/`yonerai.search-result.v1` wire testが合格すること。
2. Gateway image buildのimport checkが成功し、生成image digestを記録すること。
3. rootlessまたは同等のtrusted container runtimeで、network分離、read-only root、
   tmpfs、resource limit、cleanupを実測すること。
4. engine利用規約、network egress、運用者向けAGPL対応をowner/legalが確認すること。

## License

SearXNG本体は`AGPL-3.0-or-later`です。SearXNGを変更してnetwork経由で提供する場合、
利用者へ対応sourceを取得できる手段を提示し、copyright/license noticeを維持する
必要があります。image配布時も対応source/licenseを追跡してください。この記述は
法的助言ではなく、外部提供前にowner/legal確認が必要です。Gatewayは別serviceですが、
SearXNG imageをbaseに含むため、image全体の配布条件を別途確認します。

## 公式一次資料

- Search API: https://docs.searxng.org/dev/search_api.html
- settings location / `keep_only`:
  https://docs.searxng.org/admin/settings/settings.html
- `search.formats`: https://docs.searxng.org/admin/settings/settings_search.html
- server / limiter switch:
  https://docs.searxng.org/admin/settings/settings_server.html
- limiter / Valkey / proxy headers:
  https://docs.searxng.org/admin/searx.limiter.html
- container installation:
  https://docs.searxng.org/admin/installation-docker.html
- official Compose source:
  https://github.com/searxng/searxng/blob/master/container/docker-compose.yml
- official entrypoint:
  https://github.com/searxng/searxng/blob/master/container/entrypoint.sh
- AGPL license:
  https://github.com/searxng/searxng/blob/master/LICENSE
- fixed image evidence:
  https://hub.docker.com/layers/searxng/searxng/2026.7.26-b060c780d/images/sha256-fa1b0523e5a66c374fc04f4471f7ab54a718f33f864e8409e0db0133041eab3a
