# Self-host profiles / セルフホストprofile

公開版のself-host profileは、同じDiscord adapter、Capability Broker、artifact、
doctor契約を保ったまま、利用するprivate dependencyを明示する選択肢です。profileを
選んでもmoduleやcapabilityは自動でONになりません。

## `no_vm_local_safe`

VMを使わない最小profileです。tokenless preview、renderer doctor、audio doctor、
offline sandbox contract doctorを実行できます。外部search、media inspection、
YonerAI Coreはunavailableのままです。

## `hyperv_search`

Search GatewayとSearXNGを、Media sandboxとは別のprivate VM境界で動かす候補です。
公開assetはresource budget、pinned dependency、no-host-mount、private ingress、
bounded egressを記述します。実VM作成、container起動、query、BOT到達は未検証です。

## `hyperv_media`

公開media URLからsubtitle、metadata、thumbnail OCRを得るためのtyped Capability
Broker境界です。forced command、framed request、identity/policy digest、
cleanup-confirmed receiptを要求します。実VM、SSH identity、network policy、workerの
live状態は公開版では未検証です。

## `hybrid_local_core`

Search/Mediaなどのprivate capabilityをlocal側に残し、reasoningを将来のYonerAI Core
へ委譲するprofileです。local capabilityは選択したsandbox profileのreadinessを別途
満たす必要があります。strict Core harnessはoffline実装ですが、real Core endpoint、
credential、live continuationは未接続です。明示nonlocal profileでLocalへ暗黙fallback
しません。

## 安全な開始順

1. `PUBLIC_SELF_HOST_PROFILES.json`を読み、必要resourceとblockerを確認する。
2. tokenless doctorを実行する。
3. 対象profileのread-only preflightを実行する。
4. ownerがmutation内容とrollbackを確認する。
5. 対象module/capabilityを明示的にONにする。
6. fresh readiness、binding、cleanup receiptを確認する。

doctorやpreflightの成功だけで`live_verified=true`へ変更しないでください。live evidenceは
対象環境のcurrent truthへ別途記録します。
