[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SandboxRoot,
    [Parameter(Mandatory = $true)][string]$AutoinstallIsoPath,
    [Parameter(Mandatory = $true)][string]$ReadyMarkerPath,
    [ValidateSet("standard", "compact")][string]$Profile = "standard"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$vmName = "YonerAI-SearchSandbox"
$switchName = "YonerAI-SearchSandbox-Switch"
$natName = "YonerAI-SearchSandbox-NAT"
$network = "172.30.241.0/28"
$hostAddress = "172.30.241.1"
$root = [IO.Path]::GetFullPath($SandboxRoot)
$iso = [IO.Path]::GetFullPath($AutoinstallIsoPath)
$marker = [IO.Path]::GetFullPath($ReadyMarkerPath)
$vhdPath = Join-Path $root "YonerAI-SearchSandbox.vhdx"
$statusPath = Join-Path $root "host-bootstrap-status.json"
$preflightScript = Join-Path $PSScriptRoot "preflight-host.ps1"
$bootstrapStarted = $false

function Write-Status {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Stage
    )
    @{
        schema = "yonerai.search-sandbox.hyperv-bootstrap.v1"
        state = $State
        stage = $Stage
        vm_name = $vmName
        timestamp = [DateTimeOffset]::UtcNow.ToString("O")
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

function Assert-ChildPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (
        $Path -eq $root -or
        -not $Path.StartsWith(
            $root + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "A provisioning path escaped the dedicated sandbox root."
    }
}

try {
    if ($Profile -ne "standard") {
        throw "Only the measured standard disk profile may bootstrap the SearchSandbox."
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (
        -not $principal.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )
    ) {
        throw "Administrator elevation is required."
    }

    $preflight = & $preflightScript `
        -SandboxRoot $SandboxRoot `
        -AutoinstallIsoPath $AutoinstallIsoPath `
        -ReadyMarkerPath $ReadyMarkerPath `
        -Profile $Profile | ConvertFrom-Json -ErrorAction Stop
    if (
        $preflight.profile -ne "standard" -or
        $preflight.available -ne $true -or
        $preflight.ready_for_owner_bootstrap -ne $true -or
        $preflight.mutation_performed -ne $false
    ) {
        throw "The standard disk budget preflight is not ready for owner bootstrap."
    }

    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "The preflighted dedicated sandbox root is unavailable."
    }
    if (
        (Get-Item -Force -LiteralPath $root).Attributes -band
        [IO.FileAttributes]::ReparsePoint
    ) {
        throw "The dedicated sandbox root must not be a reparse point."
    }
    foreach ($path in @($iso, $marker, $vhdPath, $statusPath)) {
        Assert-ChildPath -Path $path
    }
    foreach ($path in @($iso, $marker)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "A required verified autoinstall asset is unavailable."
        }
    }
    $expectedHash = (
        [IO.File]::ReadAllText($marker, [Text.Encoding]::ASCII)
    ).Trim().ToLowerInvariant()
    if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
        throw "The autoinstall ready marker is invalid."
    }
    $actualHash = (
        Get-FileHash -LiteralPath $iso -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "The autoinstall ISO hash does not match its ready marker."
    }

    Import-Module Hyper-V -ErrorAction Stop
    if (Get-VM -Name $vmName -ErrorAction SilentlyContinue) {
        throw "The reserved SearchSandbox VM already exists."
    }
    if (Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue) {
        throw "The reserved SearchSandbox switch already exists."
    }
    if (Get-NetNat -Name $natName -ErrorAction SilentlyContinue) {
        throw "The reserved SearchSandbox NAT already exists."
    }
    if (Test-Path -LiteralPath $vhdPath) {
        throw "The reserved SearchSandbox disk already exists."
    }

    $bootstrapStarted = $true
    Write-Status -State "working" -Stage "private_fabric"
    New-VMSwitch -Name $switchName -SwitchType Internal | Out-Null
    $adapter = Get-NetAdapter -Name "vEthernet ($switchName)" -ErrorAction Stop
    New-NetIPAddress `
        -InterfaceIndex $adapter.ifIndex `
        -IPAddress $hostAddress `
        -PrefixLength 28 | Out-Null
    New-NetNat `
        -Name $natName `
        -InternalIPInterfaceAddressPrefix $network | Out-Null

    Write-Status -State "working" -Stage "vm"
    $vm = New-VM `
        -Name $vmName `
        -Path $root `
        -Generation 2 `
        -MemoryStartupBytes 4GB `
        -NewVHDPath $vhdPath `
        -NewVHDSizeBytes ([UInt64]$preflight.vhd_bytes) `
        -SwitchName $switchName
    Set-VM `
        -VM $vm `
        -AutomaticCheckpointsEnabled $false `
        -CheckpointType Disabled `
        -AutomaticStartAction Nothing `
        -AutomaticStopAction ShutDown
    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
    Set-VMProcessor -VM $vm -Count 2
    Set-VMFirmware `
        -VM $vm `
        -EnableSecureBoot On `
        -SecureBootTemplate "MicrosoftUEFICertificateAuthority"
    $dvd = Add-VMDvdDrive -VM $vm -Path $iso -Passthru
    Set-VMFirmware -VM $vm -FirstBootDevice $dvd
    Set-VMNetworkAdapter `
        -VMNetworkAdapter (Get-VMNetworkAdapter -VM $vm) `
        -MacAddressSpoofing Off `
        -DhcpGuard On `
        -RouterGuard On
    Start-VM -VM $vm | Out-Null

    Write-Status -State "waiting_for_guest_install" -Stage "complete"
}
catch {
    if ($bootstrapStarted -and (Test-Path -LiteralPath $root -PathType Container)) {
        Write-Status -State "failed" -Stage "bootstrap"
    }
    throw
}
