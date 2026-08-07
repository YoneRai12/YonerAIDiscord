# Public Current Status

この文書はpublic export時点の静的実装状態です。live runtimeの状態を主張しません。

## Static implementation truth

- runtime-declared capabilities: 170
- command paths: 175
- event paths: 14
- typed planner action paths: 9
- legacy unbound count: 16
- action-only within legacy unbound: 9
- true surface-unbound: 7
- public live-verification claims: 0

## Public alpha boundaries

- Real Discord, credentials, external providers, SearchSandbox, VM, and YonerAI Core are live未検証 (live-unverified).
- YonerAI共通Execution Sandboxは`implemented_unconfigured`で、trusted data channelとruntime compositionが未接続のためexecution 0です。Discord専用VMではありません。
- Dangerous or externally impactful capabilities require explicit configuration and fresh authorization.
- Generated rows marked `integrated_offline` are not configured or production-ready claims.
- unavailable public modules: publishing.site-host
