[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Start", "Status", "Stop")]
    [string]$Action,
    [Parameter(Mandatory = $true)][string]$SandboxRoot,
    [Parameter(Mandatory = $true)][string]$AdminIdentityPath,
    [Parameter(Mandatory = $true)][string]$KnownHostsPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$guestAddress = "172.30.241.2"
$localAddress = "127.0.0.1"
$gatewayPort = 8787
$forwardSpec = "${localAddress}:${gatewayPort}:127.0.0.1:${gatewayPort}"
$root = [IO.Path]::GetFullPath($SandboxRoot)
$identity = [IO.Path]::GetFullPath($AdminIdentityPath)
$knownHosts = [IO.Path]::GetFullPath($KnownHostsPath)
$statePath = Join-Path $root "gateway-forward-state.json"
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"

function Assert-RegularFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "A required fixed forward asset is unavailable."
    }
    if ((Get-Item -Force -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "A required fixed forward asset must not be a reparse point."
    }
}

function Assert-TrustedInputs {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "The dedicated sandbox root is unavailable."
    }
    if ((Get-Item -Force -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The dedicated sandbox root must not be a reparse point."
    }
    foreach ($path in @($identity, $knownHosts, $ssh)) {
        Assert-RegularFile -Path $path
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
        throw "The fixed SearchSandbox host-key pin is invalid."
    }
}

function Get-ForwardState {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw "No lifecycle-managed SearchSandbox gateway forward is recorded."
    }
    if ((Get-Item -Force -LiteralPath $statePath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The gateway forward state must not be a reparse point."
    }
    try {
        $state = Get-Content -Raw -LiteralPath $statePath -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "The gateway forward state is invalid."
    }
    $names = @($state.PSObject.Properties.Name | Sort-Object)
    $expectedNames = @(
        "guest_address",
        "local_address",
        "local_port",
        "process_id",
        "process_start_utc",
        "schema"
    )
    if (
        (Compare-Object -ReferenceObject $expectedNames -DifferenceObject $names) -or
        $state.schema -ne "yonerai.search-sandbox.gateway-forward.v1" -or
        $state.guest_address -ne $guestAddress -or
        $state.local_address -ne $localAddress -or
        $state.local_port -ne $gatewayPort -or
        $state.process_id -isnot [long] -or
        $state.process_id -lt 1 -or
        $state.process_start_utc -isnot [string] -or
        $state.process_start_utc -notmatch "^\d{4}-\d{2}-\d{2}T"
    ) {
        throw "The gateway forward state does not match the fixed contract."
    }
    return $state
}

function Assert-ForwardProcess {
    param([Parameter(Mandatory = $true)]$State)
    try {
        $process = Get-Process -Id $State.process_id -ErrorAction Stop
        $processPath = [IO.Path]::GetFullPath($process.Path)
        $processStart = $process.StartTime.ToUniversalTime().ToString("O")
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($State.process_id)" -ErrorAction Stop
    }
    catch {
        throw "The lifecycle-managed gateway forward is unavailable."
    }
    if (
        $processPath -ne $ssh -or
        $processStart -ne $State.process_start_utc -or
        $null -eq $cim -or
        $null -eq $cim.CommandLine
    ) {
        throw "The lifecycle-managed gateway forward identity does not match."
    }
    foreach ($required in @(
        "-N",
        "-L",
        $forwardSpec,
        "ExitOnForwardFailure=yes",
        "StrictHostKeyChecking=yes",
        "HostKeyAlgorithms=ssh-ed25519",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "PasswordAuthentication=no",
        "ForwardAgent=no",
        $guestAddress
    )) {
        if (-not $cim.CommandLine.Contains($required, [StringComparison]::Ordinal)) {
            throw "The lifecycle-managed gateway forward command identity does not match."
        }
    }
}

function Assert-LoopbackListener {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalAddress $localAddress -LocalPort $gatewayPort -ErrorAction Stop
    )
    if ($listeners.Count -ne 1) {
        throw "The lifecycle-managed gateway forward is not listening on the fixed loopback origin."
    }
}

function Write-ForwardState {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)
    $temporary = Join-Path $root ("gateway-forward-" + [Guid]::NewGuid().ToString("N") + ".json")
    try {
        [ordered]@{
            schema = "yonerai.search-sandbox.gateway-forward.v1"
            process_id = $Process.Id
            process_start_utc = $Process.StartTime.ToUniversalTime().ToString("O")
            guest_address = $guestAddress
            local_address = $localAddress
            local_port = $gatewayPort
        } | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8 -NoNewline
        Move-Item -LiteralPath $temporary -Destination $statePath -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

Assert-TrustedInputs

switch ($Action) {
    "Start" {
        if (Test-Path -LiteralPath $statePath) {
            throw "A gateway forward state already exists; refuse to replace it."
        }
        if (@(Get-NetTCPConnection -State Listen -LocalAddress $localAddress -LocalPort $gatewayPort -ErrorAction SilentlyContinue).Count -ne 0) {
            throw "The fixed loopback gateway origin is already occupied."
        }
        $arguments = @(
            "-F", "NUL", "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "UserKnownHostsFile=$knownHosts",
            "-o", "GlobalKnownHostsFile=NUL",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "KexAlgorithms=curve25519-sha256",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PreferredAuthentications=publickey",
            "-o", "ForwardAgent=no",
            "-o", "GatewayPorts=no",
            "-o", "PermitLocalCommand=no",
            "-o", "PermitRemoteOpen=none",
            "-o", "RequestTTY=no",
            "-o", "UpdateHostKeys=no",
            "-o", "VerifyHostKeyDNS=no",
            "-o", "ClearAllForwardings=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            "-o", "ConnectionAttempts=1",
            "-o", "ConnectTimeout=15",
            "-i", $identity,
            "-L", $forwardSpec,
            "-l", "yonerai-admin",
            $guestAddress
        )
        $process = Start-Process -FilePath $ssh -ArgumentList $arguments -PassThru -WindowStyle Hidden
        Start-Sleep -Milliseconds 500
        try {
            $state = [pscustomobject]@{
                process_id = [long]$process.Id
                process_start_utc = $process.StartTime.ToUniversalTime().ToString("O")
            }
            Assert-ForwardProcess -State $state
            Assert-LoopbackListener
            Write-ForwardState -Process $process
        }
        catch {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
            }
            throw
        }
        [pscustomobject]@{
            schema = "yonerai.search-sandbox.gateway-forward.v1"
            state = "started"
            local_origin = "http://127.0.0.1:8787"
        } | ConvertTo-Json -Compress
    }
    "Status" {
        $state = Get-ForwardState
        Assert-ForwardProcess -State $state
        Assert-LoopbackListener
        [pscustomobject]@{
            schema = "yonerai.search-sandbox.gateway-forward.v1"
            state = "running"
            local_origin = "http://127.0.0.1:8787"
        } | ConvertTo-Json -Compress
    }
    "Stop" {
        $state = Get-ForwardState
        Assert-ForwardProcess -State $state
        Stop-Process -Id $state.process_id -ErrorAction Stop
        Wait-Process -Id $state.process_id -Timeout 10 -ErrorAction Stop
        if (Get-Process -Id $state.process_id -ErrorAction SilentlyContinue) {
            throw "The lifecycle-managed gateway forward did not stop."
        }
        if (@(Get-NetTCPConnection -State Listen -LocalAddress $localAddress -LocalPort $gatewayPort -ErrorAction SilentlyContinue).Count -ne 0) {
            throw "The fixed loopback gateway origin remained occupied after stop."
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction Stop
        [pscustomobject]@{
            schema = "yonerai.search-sandbox.gateway-forward.v1"
            state = "stopped"
        } | ConvertTo-Json -Compress
    }
}
