# Search sandbox / 検索sandbox

`hyperv_search`は、private Search GatewayとSearXNG discovery serviceを既存Media VMへ
同居させないための独立profileです。Search Gatewayだけをhost側のprivate transportへ
公開し、SearXNGを直接public inboundへ公開しません。

## 公開版に含まれる境界

- pinned SearXNG/container provenanceとlicense notice
- Search GatewayとSearXNGのprocess/network分離
- read-only root、tmpfs、no persistent query volume
- host mount、Discord/OpenAI credential、`.env`のguest注入なし
- Gatewayのbounded request/result contract
- engine allowlist、paid fallbackなし、OpenAI built-in searchなし
- Generation 2 VM候補: 4 GiB memory、2 vCPU、32 GiB dynamic VHDX
- measured required free space: 36 GiB

resource budgetは`tools/hyperv-search-sandbox/disk-budget.json`、container境界は
`infra/search-sandbox/`がprivate source側の正本です。公開profileはreal address、
VM identity、host path、鍵を保持しません。

## Readiness

現在値は`implemented_unconfigured / live_verified=false`です。次が全部成立するまで
search capabilityはunavailableです。

1. read-only host preflight
2. owner承認済みの新規VM provisioning
3. pinned image buildとidentity記録
4. private network/no-host-mount/resource limitの実測
5. Gateway healthとbounded JSON query
6. cleanup、timeout、all-engine-failureの確認
7. BOT側fresh capability/readiness確認

`yonerai-discord-web-doctor`はoffline contractを検査できますが、実VMや実検索の成功を
主張しません。sample CIDRはofficial topologyではなく、対象環境でprivate addressingを
選び直し、preflightで固定・再検証します。

## Rollback

最初にsearch capabilityをOFFにし、lifecycleが所有するforward/containerを停止します。
VM、switch、diskの削除は自動化せず、ownerが対象identityと影響を確認して実行します。
