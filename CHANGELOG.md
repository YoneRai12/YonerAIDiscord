# Changelog / 変更履歴

公開配布物に実際に含まれる変更だけを記録します。内部ID、private path、secret、live運用証拠は記載しません。

## 0.1.0-alpha.2 — Unreleased

- 170 runtime capabilities、175 command paths、14 event pathsをcode-owned generatorから公開。
- direct surface未接続16件を、planner action接続9件と全known binding未接続7件へ分解。
- private分類、非materialize control、public materialize、generated receiptの会計を分離。
- public-safe VM/Sandboxとself-host profileをmachine-readable inventoryへ追加。
- READMEとArchitectureを実装済み・未設定・live未検証の境界に合わせて再構築。
- release assetが既に存在する場合はbyte一致を検証し、同一assetを安全に再利用。
- dangerous/external capabilityは引き続き既定OFF。実Discord、VM、provider、real Coreはlive未検証。
- formal Codex Securityは既知のUTF-8/cp932 plugin defectにより`UNEVALUATED`。

## 0.1.0-alpha.1 — 2026-07-30

- Public-safe、source-available、local-firstなalpha exportを公開。
- exact private commitから履歴・credential・official-private filesを除外して再現可能に生成。
- offline preview、doctors、package verification、CodeQL、public boundary checksを同梱。
- dangerous/external capabilityは既定OFF。
- 実Discord、音声、検索Sandbox、YonerAI Coreはlive未検証。
