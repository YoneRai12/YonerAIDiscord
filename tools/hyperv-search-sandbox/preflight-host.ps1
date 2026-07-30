[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SandboxRoot,
    [Parameter(Mandatory = $true)][string]$AutoinstallIsoPath,
    [Parameter(Mandatory = $true)][string]$ReadyMarkerPath,
    [ValidateSet("standard", "compact")][string]$Profile = "standard"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$vmName = "YonerAI-SearchSandbox"
$switchName = "YonerAI-SearchSandbox-Switch"
$natName = "YonerAI-SearchSandbox-NAT"
$root = [IO.Path]::GetFullPath($SandboxRoot)
$iso = [IO.Path]::GetFullPath($AutoinstallIsoPath)
$marker = [IO.Path]::GetFullPath($ReadyMarkerPath)
$budgetPath = Join-Path $PSScriptRoot "disk-budget.json"

function Get-ProfileBudget {
    if (-not (Test-Path -LiteralPath $budgetPath -PathType Leaf)) {
        throw "The code-owned disk budget is unavailable."
    }
    if ((Get-Item -Force -LiteralPath $budgetPath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The code-owned disk budget must not be a reparse point."
    }
    $document = Get-Content -LiteralPath $budgetPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ($document.schema -ne "yonerai.search-sandbox.hyperv-disk-budget.v1") {
        throw "The code-owned disk budget schema is invalid."
    }
    $profileBudget = $document.profiles.$Profile
    if ($null -eq $profileBudget) {
        throw "The requested disk budget profile is unavailable."
    }
    return $profileBudget
}

$budget = Get-ProfileBudget
if ($budget.available -ne $true) {
    [pscustomobject]@{
        schema = "yonerai.search-sandbox.hyperv-preflight.v1"
        profile = $Profile
        ready_for_owner_bootstrap = $false
        available = $false
        unavailable_reason = [string]$budget.unavailable_reason
        mutation_performed = $false
    } | ConvertTo-Json -Compress
    return
}
$requiredFreeBytes = [UInt64]$budget.required_free_bytes
$vhdBytes = [UInt64]$budget.vhd_bytes
if ($requiredFreeBytes -ne 36GB -or $vhdBytes -ne 32GB) {
    throw "The standard disk budget no longer matches the approved sandbox boundary."
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

foreach ($path in @($iso, $marker)) {
    Assert-ChildPath -Path $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "A required verified autoinstall asset is unavailable."
    }
    if (
        (Get-Item -Force -LiteralPath $path).Attributes -band
        [IO.FileAttributes]::ReparsePoint
    ) {
        throw "A provisioning asset must not be a reparse point."
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

$drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($root).TrimEnd("\").TrimEnd(":")) -PSProvider FileSystem
$freeBytes = [UInt64]$drive.Free
$shortfallBytes = if ($freeBytes -lt $requiredFreeBytes) {
    $requiredFreeBytes - $freeBytes
}
else {
    [UInt64]0
}

[pscustomobject]@{
    schema = "yonerai.search-sandbox.hyperv-preflight.v1"
    profile = $Profile
    ready_for_owner_bootstrap = ($shortfallBytes -eq 0)
    available = $true
    vm_name = $vmName
    iso_sha256 = "sha256:$actualHash"
    required_free_bytes = $requiredFreeBytes
    free_bytes = $freeBytes
    shortfall_bytes = $shortfallBytes
    vhd_bytes = $vhdBytes
    buckets = @($budget.buckets)
    mutation_performed = $false
} | ConvertTo-Json -Compress
