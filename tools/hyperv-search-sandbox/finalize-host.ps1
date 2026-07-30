[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SandboxRoot,
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [Parameter(Mandatory = $true)][string]$AdminIdentityPath,
    [Parameter(Mandatory = $true)][string]$KnownHostsPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$vmName = "YonerAI-SearchSandbox"
$switchName = "YonerAI-SearchSandbox-Switch"
$natName = "YonerAI-SearchSandbox-NAT"
$network = "172.30.241.0/28"
$hostAddress = "172.30.241.1"
$guestAddress = "172.30.241.2"
$root = [IO.Path]::GetFullPath($SandboxRoot)
$repository = [IO.Path]::GetFullPath($RepositoryRoot)
$identity = [IO.Path]::GetFullPath($AdminIdentityPath)
$knownHosts = [IO.Path]::GetFullPath($KnownHostsPath)
$staging = Join-Path $root ("finalize-" + [Guid]::NewGuid().ToString("N"))
$bundle = Join-Path $staging "yonerai-search-bundle.tar"
$git = "$env:ProgramFiles\Git\cmd\git.exe"
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"
$scp = "$env:WINDIR\System32\OpenSSH\scp.exe"

function Assert-RegularFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "A required regular file is unavailable."
    }
    if (
        (Get-Item -Force -LiteralPath $Path).Attributes -band
        [IO.FileAttributes]::ReparsePoint
    ) {
        throw "A required file must not be a reparse point."
    }
}

function Invoke-StrictSsh {
    param([Parameter(Mandatory = $true)][string]$Command)
    & $ssh `
        -F NUL `
        -T `
        -o BatchMode=yes `
        -o "UserKnownHostsFile=$knownHosts" `
        -o GlobalKnownHostsFile=NUL `
        -o StrictHostKeyChecking=yes `
        -o HostKeyAlgorithms=ssh-ed25519 `
        -o KexAlgorithms=curve25519-sha256 `
        -o IdentitiesOnly=yes `
        -o IdentityAgent=none `
        -o PasswordAuthentication=no `
        -o KbdInteractiveAuthentication=no `
        -o PreferredAuthentications=publickey `
        -o ForwardAgent=no `
        -o ClearAllForwardings=yes `
        -o PermitLocalCommand=no `
        -o RequestTTY=no `
        -o UpdateHostKeys=no `
        -o VerifyHostKeyDNS=no `
        -o ConnectionAttempts=1 `
        -o ConnectTimeout=15 `
        -i $identity `
        -l yonerai-admin `
        $guestAddress `
        $Command
    if ($LASTEXITCODE -ne 0) {
        throw "The fixed SearchSandbox SSH operation failed."
    }
}

try {
    if ($SourceCommit -notmatch "^[0-9a-f]{40}$") {
        throw "SourceCommit must be one lowercase full Git object ID."
    }
    foreach ($path in @($identity, $knownHosts, $git, $ssh, $scp)) {
        Assert-RegularFile -Path $path
    }
    if (
        -not (Test-Path -LiteralPath $root -PathType Container) -or
        -not (Test-Path -LiteralPath $repository -PathType Container)
    ) {
        throw "A required dedicated directory is unavailable."
    }
    foreach ($path in @($root, $repository)) {
        if (
            (Get-Item -Force -LiteralPath $path).Attributes -band
            [IO.FileAttributes]::ReparsePoint
        ) {
            throw "A trusted provisioning root must not be a reparse point."
        }
    }
    $knownHostLines = @(
        [IO.File]::ReadAllLines($knownHosts, [Text.Encoding]::ASCII) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if (
        $knownHostLines.Count -ne 1 -or
        $knownHostLines[0] -notmatch (
            "^" + [regex]::Escape($guestAddress) +
            " ssh-ed25519 [A-Za-z0-9+/]+={0,3}$"
        )
    ) {
        throw "known_hosts must contain one console-verified Ed25519 pin."
    }

    Import-Module Hyper-V -ErrorAction Stop
    $vm = Get-VM -Name $vmName -ErrorAction Stop
    if ($vm.State.ToString() -ne "Running" -or $vm.Generation -ne 2) {
        throw "The reserved running Generation 2 SearchSandbox VM is unavailable."
    }
    $switch = Get-VMSwitch -Name $switchName -ErrorAction Stop
    if ($switch.SwitchType.ToString() -ne "Internal") {
        throw "The reserved SearchSandbox switch is not internal."
    }
    $nat = Get-NetNat -Name $natName -ErrorAction Stop
    if ($nat.InternalIPInterfaceAddressPrefix -ne $network) {
        throw "The reserved SearchSandbox NAT does not match."
    }
    $hostAdapter = Get-NetAdapter -Name "vEthernet ($switchName)" -ErrorAction Stop
    $hostAddresses = @(
        Get-NetIPAddress `
            -InterfaceIndex $hostAdapter.ifIndex `
            -AddressFamily IPv4 `
            -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -eq $hostAddress -and $_.PrefixLength -eq 28
        }
    )
    if ($hostAddresses.Count -ne 1) {
        throw "The host-side SearchSandbox address does not match."
    }
    $vmAdapters = @(Get-VMNetworkAdapter -VM $vm -ErrorAction Stop)
    if (
        $vmAdapters.Count -ne 1 -or
        $vmAdapters[0].SwitchName -ne $switchName -or
        $vmAdapters[0].MacAddressSpoofing.ToString() -ne "Off"
    ) {
        throw "The SearchSandbox VM network boundary does not match."
    }
    $switchUsers = @(
        Get-VM -ErrorAction Stop |
        ForEach-Object { Get-VMNetworkAdapter -VM $_ -ErrorAction Stop } |
        Where-Object { $_.SwitchName -eq $switchName }
    )
    if ($switchUsers.Count -ne 1 -or $switchUsers[0].VMName -ne $vmName) {
        throw "The SearchSandbox internal switch is not dedicated."
    }

    New-Item -ItemType Directory -Path $staging | Out-Null
    & $git -C $repository cat-file -e "$SourceCommit^{commit}"
    if ($LASTEXITCODE -ne 0) {
        throw "The requested source commit is unavailable."
    }
    & $git `
        -C $repository `
        archive `
        --format=tar `
        "--output=$bundle" `
        $SourceCommit `
        infra/search-sandbox `
        src/yonerai_discord `
        tools/hyperv-search-sandbox/guest
    if ($LASTEXITCODE -ne 0) {
        throw "The code-owned SearchSandbox bundle could not be created."
    }
    Assert-RegularFile -Path $bundle
    $bundleHash = (
        Get-FileHash -LiteralPath $bundle -Algorithm SHA256
    ).Hash.ToLowerInvariant()

    & $scp `
        -F NUL `
        -o BatchMode=yes `
        -o "UserKnownHostsFile=$knownHosts" `
        -o GlobalKnownHostsFile=NUL `
        -o StrictHostKeyChecking=yes `
        -o HostKeyAlgorithms=ssh-ed25519 `
        -o KexAlgorithms=curve25519-sha256 `
        -o IdentitiesOnly=yes `
        -o IdentityAgent=none `
        -o PasswordAuthentication=no `
        -o KbdInteractiveAuthentication=no `
        -o ClearAllForwardings=yes `
        -i $identity `
        $bundle `
        "yonerai-admin@${guestAddress}:/tmp/yonerai-search-bundle.tar"
    if ($LASTEXITCODE -ne 0) {
        throw "The fixed SearchSandbox bundle upload failed."
    }

    $remoteStage = "/var/tmp/yonerai-search-stage/$SourceCommit"
    $remoteCommand = (
        "set -Eeuo pipefail; " +
        "test `"`$(sha256sum /tmp/yonerai-search-bundle.tar | awk '{print `$1}')`" = `"$bundleHash`"; " +
        "sudo /usr/bin/install -d -o root -g root -m 0700 `"$remoteStage`"; " +
        "sudo /usr/bin/tar --no-same-owner --no-same-permissions -xf /tmp/yonerai-search-bundle.tar -C `"$remoteStage`"; " +
        "sudo /usr/bin/env YONERAI_SOURCE_COMMIT=$SourceCommit /bin/bash " +
        "`"$remoteStage/tools/hyperv-search-sandbox/guest/provision.sh`"; " +
        "rm -f /tmp/yonerai-search-bundle.tar"
    )
    Invoke-StrictSsh -Command $remoteCommand

    $forwardScript = Join-Path $PSScriptRoot "gateway-forward.ps1"
    & $forwardScript `
        -Action Start `
        -SandboxRoot $root `
        -AdminIdentityPath $identity `
        -KnownHostsPath $knownHosts
    if ($LASTEXITCODE -ne 0) {
        throw "The lifecycle-managed SearchSandbox gateway forward could not start."
    }
    try {
        & (Join-Path $PSScriptRoot "smoke-owner.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "The SearchSandbox owner smoke failed."
        }
    }
    catch {
        & $forwardScript `
            -Action Stop `
            -SandboxRoot $root `
            -AdminIdentityPath $identity `
            -KnownHostsPath $knownHosts
        throw
    }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
