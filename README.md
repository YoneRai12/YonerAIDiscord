# YonerAIDiscord

日本語: これは公式のpublic-safe、local-first、self-host向けreference editionです。公式hosted overlayはこの公開配布物に含まれません。

English: This is the official public-safe, local-first, self-host reference edition. The official hosted overlay is not included in this public release.

日本語: この配布物は、YonerAI Discord Suite の公開可能なソースと文書です。これは source-available（ソース閲覧可）であり、自由な商用利用を許可するオープンソースライセンスではありません。コードにはPolyForm Noncommercial、文書・素材にはCC BY-NC-NDが適用され、ブランドはAll Rights Reservedです。危険または外部へ影響する機能は既定で OFF です。

English: This release contains the publicly shareable source and documentation for YonerAI Discord Suite. It is source-available, not an open-source grant for unrestricted commercial use. Code uses PolyForm Noncommercial, documentation and assets use CC BY-NC-ND, and the brand is All Rights Reserved. Dangerous or externally impactful features are OFF by default.

## What this is / 何者か

日本語: YonerAI Discord Suiteは、Discord上の運用機能を安全な能力境界で扱うためのモジュール型ソフトウェアです。公開版で今すぐ使えるのは、ソース閲覧、オフライン検証、local preview、および明示承認前のpreflightです。実Discord・音声・検索sandbox・YonerAI Coreへの接続は既定OFFで、live未検証です。

English: YonerAI Discord Suite is modular software for handling Discord operations behind explicit capability boundaries. This public release currently supports source inspection, offline verification, local preview, and preflight before explicit approval. Real Discord, voice, search sandbox, and YonerAI Core connections are OFF by default and not live-verified.

## Status / 状態

日本語: 公開配布物はオフライン検証を前提にしています。実Discord、認証情報、外部プロバイダ、運用環境での動作は live未検証です。

English: The public package is intended for offline verification. It is not live-verified against Discord, credentials, external providers, or an operating deployment.

## Safe start / 安全な開始

1. Read `PUBLIC_BOUNDARY.md` and `SECURITY.md`.
2. Keep all integrations disabled until their owner has reviewed the configuration.
3. Never publish credentials, production identifiers, private paths, or live operational evidence.

## Five-minute local preview / 5分のlocal preview

Python import名は`yonerai_discord`、互換distribution名は`yonerai-discord-suite`です。

The Python import name is `yonerai_discord`; the compatible distribution name is `yonerai-discord-suite`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.lock
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m yonerai_discord.offline_preview --format json --pretty
```

POSIXではinterpreterを`.venv/bin/python`へ読み替えてください。`.env`は作らず、tokenも貼り付けないでください。
This preview is local and tokenless; success is offline evidence only, not a live-readiness claim.

## License, security, and Core future / ライセンス・安全性・Coreの将来

Read `LICENSE` for the code, documentation/assets, and brand terms, and read `SECURITY.md` before reporting a concern. YonerAI Core integration is a future owner-approved direction only; it is not connected or live-verified by this public release.
