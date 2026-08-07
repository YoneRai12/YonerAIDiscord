# Media sandbox / Media解析sandbox

`hyperv_media`は、明示された公開media URLのsubtitle、metadata、thumbnail OCRを
Capability Broker経由で実行する独立profileです。host shell、任意command、任意URL、
download配布、host filesystemをmodelへ公開しません。

## 公開版に含まれる境界

- `media.url-inspect` / `cap-run-media-url-inspection`
- BOT_OWNER、HIGH risk、module/capability既定OFF
- actor/guild/conversation bindingとidempotency
- fixed framed request、forced-command worker
- backend identity、effective policy digest、cleanup receipt
- input/output/wall-time resource limit
- public media hostのcode-owned allowlist
- private/special destination拒否とno host mount
- Generation 2 VM候補: 16 GiB memory、2 vCPU、20 GiB dynamic VHDX

private source側の正本は`tools/hyperv-media-inspection/`、
`yonerai_discord.modules.media_inspection.hyperv_contract`、Capability Brokerです。
公開profileはVM identity、address、鍵、host path、media本文を保持しません。

## Readiness

現在値は`implemented_unconfigured / live_verified=false`です。offline
`yonerai-discord-sandbox-doctor`はtyped status、binding、artifact ownership、
audit、timeout/cancel cleanupをinjected contractで検査します。actual VM、SSH、
network egress、media extraction、Discord deliveryのlive成功ではありません。

利用前には、read-only preflight、VM/worker identity、effective network policy、
cleanup、fresh authorization、module/capability readinessを対象環境で確認します。
失敗、timeout、cancel、identity差替え、cleanup未確認ではartifactを受理しません。

## Rollback

media capabilityをOFFにし、BOTが所有するrequest/processだけを停止します。VM、switch、
disk、operator secretの削除・変更は自動化しません。既存queueや他guildのstateを
巻き戻しません。
