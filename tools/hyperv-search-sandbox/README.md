# Hyper-V Search Sandbox offline provisioning

`YonerAI-SearchSandbox`を既存`YonerAI-MediaSandbox`から分離し、private Search Gatewayと
SearXNGだけを動かすためのoperator assetです。tracked codeはVM名、private fabric、
guest address、bundle範囲、health/search smokeを固定します。秘密値、Discord/OpenAI
credential、`.env`、host mountは渡しません。

## 固定境界

- VM: `YonerAI-SearchSandbox`（Generation 2、Secure Boot）
- switch/NAT: 専用internal switch、`172.30.241.0/28`
- host/guest: `172.30.241.1` / `172.30.241.2`
- inbound: hostからguestのSSHとSearch Gateway `8787`だけ
- Search Gateway: guest loopbackとprivate guest addressだけ
- SearXNG: host portなし、engine allowlistだけがegressを持つ
- VM resource候補: 4 GiB、2 vCPU、32 GiB dynamic VHDX
- no Enhanced Session、GPU、SMB、clipboard、host volume、Discord/OpenAI secret
- paid fallback: 0、OpenAI built-in web search call: 0

このassetは2026-07-29時点でoffline検証のみです。VM作成、Ubuntu install、container
build、実検索、BOT接続、live readinessは実行・確認していません。

## 手順

1. `preflight-host.ps1`を通常権限で実行し、Hyper-V availability、固定asset、予約名、
   ISO/marker、空き容量をread-only確認する。
2. ownerが管理者PowerShellから`bootstrap-host.ps1`を一度だけ実行する。これは専用
   switch/NAT/VMを新規作成し、検証済みautoinstall ISOをbootする。既存同名resourceが
   1件でもあればfail closedする。
3. Ubuntu install後、consoleで確認したEd25519 host keyを1行だけ持つ`known_hosts`と
   admin private keyを用意する。`finalize-host.ps1`はTOFUをせずstrict pinを使い、
   指定commitの`git archive`からcode-owned subsetだけをguestへ転送する。
4. finalizeは`guest/provision.sh`を固定引数で実行し、rootless Podman service、
   private bind、health、実JSON queryを検査する。
5. 以後のone-click確認は`smoke-owner.ps1`を使う。query、URL、source本文はreceiptへ
   保存せず、件数とcode-owned fee/tool assertionだけを出す。

例（machine固有pathはoperatorが明示する）:

```powershell
.\preflight-host.ps1 `
  -SandboxRoot <dedicated-root> `
  -AutoinstallIsoPath <verified-autoinstall.iso> `
  -ReadyMarkerPath <verified-autoinstall.iso.ready>

# owner approval後だけ
.\bootstrap-host.ps1 `
  -SandboxRoot <dedicated-root> `
  -AutoinstallIsoPath <verified-autoinstall.iso> `
  -ReadyMarkerPath <verified-autoinstall.iso.ready>

.\finalize-host.ps1 `
  -SandboxRoot <dedicated-root> `
  -RepositoryRoot <clean-checkout> `
  -SourceCommit <40-hex-commit> `
  -AdminIdentityPath <admin-ed25519-private-key> `
  -KnownHostsPath <pinned-known-hosts>

.\smoke-owner.ps1
```

`bootstrap-host.ps1`はPC再起動、Windows optional feature有効化、既存VM停止/削除を
行いません。Hyper-Vが未導入ならpreflightでblockerを返します。autoinstall ISOの生成、
host-keyのconsole照合、管理者操作はowner laneです。scriptをrepoへ置いただけでは
SearchSandbox ready、zero-cost実検索、BOT liveを主張しません。

## Provenance / license

SearXNG/image/Ubuntu pinは`infra/search-sandbox/VERSION.lock`、第三者licenseと変更境界は
`infra/search-sandbox/LICENSES.md`が正本です。SearXNGは`AGPL-3.0-or-later`です。
public network提供やimage配布、upstream source変更はowner/legal review前に行いません。
