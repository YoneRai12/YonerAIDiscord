# Architecture / アーキテクチャ

日本語: 公開配布物は、入力を検証し、能力・設定の境界を通じて処理し、出力を安全なsinkへ渡す構成を説明します。実運用の接続情報、identity、host、private path、live構成は公開しません。

English: The public package describes a flow that validates input, crosses capability and configuration boundaries, and sends output to safe sinks. It does not disclose production connection details, identities, hosts, private paths, or live topology.

```text
input -> validation -> capability/configuration guard -> service -> safe output
```

External effects require an explicit opt-in and owner-controlled configuration; the public default is OFF.
