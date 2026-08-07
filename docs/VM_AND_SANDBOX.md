# VM and sandbox profiles / VM・sandbox profile

この公開版は、VMやsandboxを「存在するだけで安全・稼働中」とは扱いません。
`PUBLIC_SELF_HOST_PROFILES.json`が公開profileの機械可読正本です。全profileの
`live_verified`は`false`で、ownerがread-only preflight、identity、policy、
cleanupを対象環境で確認するまで外部capabilityは利用不可です。

## 共通境界

- preflightはread-onlyで、VM、switch、NAT、container、鍵、設定を自動作成しません。
- public inbound、host mount、clipboard、GPU passthrough、host secretのguest注入は
  許可しません。
- networkはcode-owned allowlistと実行時の再検証が必要です。文書中の例示networkや
  sample CIDRを正式topologyとして採用しません。
- VM名、address、host path、鍵、machine identityはoperator環境の非公開情報です。
  公開profileへ固定しません。
- doctor成功はcontract/offline evidenceであり、actual VM readinessやlive成功では
  ありません。
- timeout、cancel、failureはworker停止とworkspace cleanupが確認できなければ成功に
  昇格しません。
- rollbackはまずcapabilityをOFFにし、softwareが所有するsessionだけを停止します。
  VMやdiskの削除はownerの明示操作なしに行いません。

## Profileの選択

- `no_vm_local_safe`: tokenless previewとdoctorだけ。VMやexternal providerを使いません。
- `hyperv_search`: private Search GatewayとSearXNG用の独立VM境界です。
- `hyperv_media`: 公開media URLのsubtitle/metadata/OCR用の独立VM境界です。
- `hyperv_execution_sandbox`: YonerAI共通の使い捨てpure execution境界です。Discord専用ではなく、
  network adapter 0、IP/DNS/Web routeなし、host mount/secretなし、公開capability 0です。
- `hybrid_local_core`: local/private capabilityと将来Core reasoningを組み合わせる境界です。
  real Core endpointは公開版で未接続です。

`hyperv_search`と`hyperv_media`は別のtrust boundaryです。同居、暗黙fallback、WSLやhost
processへの置換を行いません。どのprofileもmodule/capabilityの既定OFF、fresh
authorization、typed receipt、artifact ownershipを迂回しません。

`hyperv_execution_sandbox`は`implemented_unconfigured`です。trusted data channel、固定broker、
base image digest、runtime compositionが揃うまで`trusted_data_channel_unimplemented`でfail closedし、
executionは0です。Stage 1はVM作成済み・live ready・artifact delivery済みを意味しません。
