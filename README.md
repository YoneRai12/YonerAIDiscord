# YonerAIDiscord

Public-safe、local-first、self-host向けのDiscord capability platformです。Discordの入力を直接providerへ流さず、RBAC・同意・fresh authorization・idempotency・監査・typed artifact境界を通して、AI、検索、音声、media、VM/Sandbox、将来のYonerAI Coreを同じCapability APIから扱います。

This is the official public-safe, local-first, self-host reference edition. The official hosted overlay is not included; production identities, credentials, and private operations are also excluded.

Pythonのimport packageは`yonerai_discord`、配布名は`yonerai-discord-suite`です。

> **Alpha truth:** 実装・設定・live検証を分離しています。公開alphaに含まれるコードやoffline testは、実Discord、実VM、実provider、real YonerAI Coreでの成功を意味しません。外部影響のある機能は既定で OFFです。

## 30秒で分かる機能

- Discord: slash、mention、reply、button/modalを能力・権限境界の内側で処理
- AI: provider-neutral routing、bounded planner、同一scope会話、添付、progress/final renderer
- Search: YonerAI Search Fabric、evidence fetch/classification/citation、paid fallback既定禁止
- Audio: local library、queue、playlist、VOICEVOX、TTS ducking、restart-safe projection
- Media: image/video/music contracts、artifact store、durable Discord delivery
- Operations: moderation、schedule、community、system status、doctor、audit
- Sandbox: Capability Broker、Browser Sandbox、optional Hyper-V Search/Media profiles、networkなしのYonerAI共通Execution Sandbox Stage 1
- Core: Local / Direct Core / Processing API seam / Hybridを同じExecutionGatewayで切替

## Current alpha status

| 項目 | 公開正本 |
| --- | --- |
| runtime-declared capabilities | **177** |
| command paths | **182** |
| event paths | **14** |
| direct command/event/model-tool未接続runtime | **16** |
| planner actionを含む全known binding未接続runtime | **7** |
| public live-verification claim | **0** |

`656 historical canonical`と`833 registry total`は履歴・静的registryの母集団であり、現在liveで使える機能数ではありません。全177件の状態は[`PUBLIC_CAPABILITY_MATRIX.json`](PUBLIC_CAPABILITY_MATRIX.json)、概要は[`PUBLIC_CAPABILITY_SUMMARY.md`](PUBLIC_CAPABILITY_SUMMARY.md)、command一覧は[`PUBLIC_COMMAND_INDEX.md`](PUBLIC_COMMAND_INDEX.md)を参照してください。

## Architecture

```text
Discord Surface
  → Admission / RBAC / Consent
  → Capability API
  → ExecutionGateway
      ├─ Local
      ├─ Direct YonerAI Core
      ├─ Discord Processing API seam
      └─ Hybrid
  → Provider / Search / Audio / VM / Files / Memory
  → Approval / Audit / Status
  → Discord Renderer
```

詳細なpermission、artifact、VM、失敗時のflowは[`ARCHITECTURE.md`](ARCHITECTURE.md)にあります。

## Safe start lanes

### Lane A — Tokenless Preview

Discord tokenや`.env`を使わず、production payload builderとdoctorを確認します。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.lock
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m yonerai_discord.offline_preview --format json --pretty
.\.venv\Scripts\python.exe scripts\discord_renderer_doctor.py
```

### Lane B — Local Self-Host Discord

1. `.env.example`を`.env`へコピーする。
2. Discord token、owner/guild設定だけを入力する。
3. external provider、VM、remote CoreをOFFのままpreflightする。
4. 合格後に必要なcapabilityだけを明示的にONにする。

```powershell
.\.venv\Scripts\python.exe scripts\runtime_preflight.py
.\.venv\Scripts\yonerai-discord.exe
```

tokenをGit、ログ、issue、スクリーンショットへ載せないでください。public alphaでは実Discord接続をlive verifiedとは主張していません。

### Lane C — Optional Voice / Search / VM

- VOICEVOX: loopback `127.0.0.1`、remote OFF、managed process OFFが安全な初期値
- Search: private SearXNG + YonerAI Search Gateway、raw query retention 0、paid fallback OFF
- Hyper-V: preflightはread-only。同名resourceや既存switch/NATを変更せず、新規dedicated resourceだけをowner acknowledgement後に作成

VM要件とprofileは[`docs/VM_AND_SANDBOX.md`](docs/VM_AND_SANDBOX.md)と[`PUBLIC_SELF_HOST_PROFILES.json`](PUBLIC_SELF_HOST_PROFILES.json)を参照してください。

YonerAI共通Execution Sandbox Stage 1はDiscord専用VMではありません。公開alphaでは
`implemented_unconfigured`、capability 0です。trusted data channelのコード境界は
`implemented_offline`で、offline runtime compositionは接続済みですが、protected broker/workerへの
runtime/live接続と実VM readinessが未成立なので
execution 0です。VM作成済み・利用可能とは主張しません。

## Search Fabric

標準routeは`web.search.evidence`です。検索候補を取得しただけで事実とせず、SafeEvidenceFetcherが取得できたURLだけをcitationへ採用します。source class、取得時刻、重複・矛盾、corroborationを保持します。

OpenAI built-in searchは別capability `web.search.openai_paid`で、既定OFF・自動fallback禁止です。local search失敗時も勝手に有料検索へ切り替えません。

## Audio and VOICEVOX

既存Audio Coreを正本として、guildごとのqueue、playlist、loop/volume、local rights ledger、VOICEVOX TTS、DuckingMixerを提供します。signed URL、authorization header、cookie、private absolute pathは永続queueへ保存しません。実VC、FFmpeg、VOICEVOXの環境成功は各doctorと明示smokeで別途確認してください。

## YonerAI Core and profiles

Internal Run API v0.1のstrict local harness、SSE、`/v1/runs/{run_id}/results`、ref-only Files境界は実装されています。real YonerAI Core origin、credential、identityは未接続です。未設定のDirect Core/Hybrid profileをLocalへ黙ってfallbackさせません。

## Security and public/private boundary

- dangerous/external capabilities: default OFF
- fresh actor/module/capability/target authorization at side-effect boundaries
- secret-like input、private path、raw tokenを通常log/audit/detailsへ保存しない
- artifactはscope-bound opaque refで受け渡す
- public exportはexact private commitから履歴なしで再生成する
- official hosted overlay、real IDs、live DB/log、credential、production topologyはprivate

公開範囲の会計は[`PUBLIC_EXPORT_COVERAGE.md`](PUBLIC_EXPORT_COVERAGE.md)と[`PUBLIC_EXCLUSION_MATRIX.json`](PUBLIC_EXCLUSION_MATRIX.json)を参照してください。

## Tests and CI

Public CIはLinux unit/integration、package build、offline wheel verification、generated truth drift、boundary/secret checks、CodeQLを実行します。green testは対象contractのoffline証拠であり、実Discord・VM・provider・Coreのlive readinessを証明しません。

## License and roadmap

本配布はsource-availableです。コードはPolyForm Noncommercial、文書・素材はCC BY-NC-ND、ブランドはAll Rights Reservedです。第三者dependencyのlicenseは[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)と[`DEPENDENCY_LICENSES.json`](DEPENDENCY_LICENSES.json)を参照してください。

既知の未接続・Owner作業・external blockerは[`PUBLIC_CURRENT_STATUS.md`](PUBLIC_CURRENT_STATUS.md)と[`ROADMAP.md`](ROADMAP.md)に、release差分は[`CHANGELOG.md`](CHANGELOG.md)に記録します。
