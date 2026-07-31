# Architecture / アーキテクチャ

この文書はpublic-safe reference editionのcode-owned境界を説明します。production identity、host、private path、credential、official routing inventoryは含みません。

## Canonical flow

```text
Discord Surface / Future Surface
                │
                ▼
Admission ─ RBAC ─ Consent ─ Fresh authorization
                │
                ▼
       YonerAI Capability API
                │
                ▼
         ExecutionGateway
        ┌───────┼────────┬─────────┐
        │       │        │         │
      Local  Direct   Processing  Hybrid
             Core      API seam
        │       │        │         │
        └───────┴────────┴─────────┘
                │
                ▼
Provider / Search / Audio / VM / Files / Memory
                │
                ▼
Approval / Append-only Audit / Status / Current Truth
                │
                ▼
        Discord Renderer / Artifact delivery
```

Discord adapterやcommand handlerはprovider SDK、raw DB、host shell、private Core JSONを直接呼びません。domain serviceとversioned adapterを境界にします。

## Permission flow

1. entryでguild/channel/thread/DM、actor、message、module/capability、RBACを確認する。
2. consentやapprovalが必要なら、actor・scope・target・args digest・expiryへ束縛する。
3. queue待機、external await、artifact read後にfresh memberと現在policyを再確認する。
4. side effect直前にmodule/plugin/service identity、closing、target stateを再確認する。
5. Discord送信直前にも現在のsend権限を確認する。

途中のrole剥奪、module OFF、service差替え、target削除はfail closedです。cached memberや初回snapshotを最終authorityにしません。

## Data and privacy flow

- Discord本文や検索queryは必要最小限だけ下流へ渡す。
- private memoryは明示policyなしで外部search queryへ混ぜない。
- raw query retentionは既定0。
- secret、token、cookie、authorization header、private absolute pathを通常log/auditへ保存しない。
- auditはcontent-free digest、scope、state transition、opaque referenceを優先する。

## Artifact flow

```text
Validated input
  → Domain/Provider
  → scoped ArtifactReference
  → Artifact Store
  → validator + hash + ownership check
  → prepared attachment
  → exact Discord message delivery
  → accepted/uncertain/failed receipt
```

local artifact IDをCore-readable refと偽りません。Core/Discord/VM間はversioned ref mappingを通し、raw host pathや公開URLへ自動変換しません。`generated`と`delivered`は別状態です。

## Search flow

```text
web.search.evidence
  → YonerAI Search Gateway
  → SearXNG / official metadata candidates
  → SSRF-safe evidence fetch
  → source classification / corroboration / cache
  → evidence-only AI synthesis
  → verified numbered citations
```

OpenAI built-in web searchは別capabilityで既定OFFです。標準search失敗時のpaid fallbackはありません。

## Audio flow

```text
Discord command/mention
  → Music/Voice service
  → durable guild queue projection
  → local/authorized source resolution
  → GuildAudioSession
  → PCM mixer + TTS ducking
  → Discord voice
```

queueへはstable local refとrequester/policyだけを保存し、signed stream URL、credential、cookie、private pathは保存しません。1 guild 1 voice connectionを既存Audio Coreが管理します。

## VM and Sandbox flow

```text
Typed capability request
  → Capability Broker
  → policy/resource/network validation
  → dedicated Managed Sandbox
  → bounded receipt + artifact descriptors
  → cleanup/destroy confirmation
```

public defaultはread-only preflightです。host shell、arbitrary command、host filesystem mount、Discord/OpenAI secret、public inboundをSandboxへ公開しません。Hyper-V resource作成はoperator acknowledgement後の新規dedicated resourceに限定し、同名既存resourceは変更しません。

## Topologies

- **Local:** reasoningとcapabilityをlocal/self-hostで処理
- **Direct Core:** versioned Internal Run API v0.1を使用
- **Processing API seam:** production endpoint未確定の中立境界
- **Hybrid:** private/local capability実行とshared reasoningを分離

明示したnonlocal topologyがunconfiguredならfail closedです。Localへの暗黙fallbackはしません。

## Hosting profiles

- Official Managed Cloud
- Official Hybrid Private
- Full Private Self-Host

Public alphaはlocal/self-host referenceであり、official hosted deploymentを含みません。profile切替はcompositionで行い、Discord commandやrendererを作り直しません。

## Failure, retry, and rollback

- idempotency keyでduplicate provider/sink実行を防ぐ
- terminal `final|error`はexactly once
- external side effect開始後のtimeout/cancelは`outcome_uncertain`を区別する
- durable jobはcheckpoint/resumeとstale/zombie update拒否を持つ
- retryはsame scope/digest/target bindingを再検証する
- rollbackを実行していないreceiptで「rollback済み」と主張しない

## Public/private split

Publicにはneutral contracts、local execution、tests、generic VM/Sandbox assets、doctors、self-host packagingを含めます。official credentials、real IDs、Cloudflare bindings、production DB/log、official topology、private runbookは含めません。再現可能な一覧は`PUBLIC_EXPORT_COVERAGE.md`と`PUBLIC_EXCLUSION_MATRIX.json`を正本にします。
