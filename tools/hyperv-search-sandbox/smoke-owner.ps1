[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$origin = [Uri]"http://127.0.0.1:8787"
$handler = [Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$handler.AllowAutoRedirect = $false
$client = [Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(12)

function ConvertTo-CanonicalQueryDigest {
    param([Parameter(Mandatory = $true)][string]$Query)
    $bytes = [Text.Encoding]::UTF8.GetBytes(
        "yonerai.search-query.v1" + [char]0 + $Query
    )
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $value = (
            $sha256.ComputeHash($bytes) |
            ForEach-Object { $_.ToString("x2") }
        ) -join ""
        return "sha256:$value"
    }
    finally {
        $sha256.Dispose()
    }
}

try {
    $health = $client.GetAsync(
        [Uri]::new($origin, "/healthz"),
        [Net.Http.HttpCompletionOption]::ResponseHeadersRead
    ).GetAwaiter().GetResult()
    try {
        if (-not $health.IsSuccessStatusCode) {
            throw "Search Gateway health probe failed."
        }
        $healthBody = $health.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($healthBody.Length -lt 1 -or $healthBody.Length -gt 4096) {
            throw "Search Gateway health body is outside the bounded contract."
        }
        $healthDocument = [Text.Encoding]::UTF8.GetString($healthBody) | ConvertFrom-Json
        if (
            $healthDocument.schema -ne "yonerai.search-health.v1" -or
            $healthDocument.backend_id -ne "searxng.local" -or
            $healthDocument.ready -ne $true
        ) {
            throw "Search Gateway health contract did not report ready."
        }
    }
    finally {
        $health.Dispose()
    }

    $query = "SearXNG documentation"
    $requestId = "owner-smoke-" + [Guid]::NewGuid().ToString("N")
    $body = @{
        schema = "yonerai.search-request.v1"
        request_id = $requestId
        query = $query
        query_digest = ConvertTo-CanonicalQueryDigest -Query $query
        intent = "official"
        language = "en"
        limit = 3
    } | ConvertTo-Json -Compress
    $content = [Net.Http.StringContent]::new(
        $body,
        [Text.Encoding]::UTF8,
        "application/json"
    )
    $response = $client.PostAsync(
        [Uri]::new($origin, "/v1/search"),
        $content
    ).GetAwaiter().GetResult()
    try {
        if (-not $response.IsSuccessStatusCode) {
            throw "Search Gateway live JSON query failed."
        }
        $responseBody = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($responseBody.Length -lt 1 -or $responseBody.Length -gt 524288) {
            throw "Search Gateway response is outside the bounded contract."
        }
        $document = [Text.Encoding]::UTF8.GetString($responseBody) | ConvertFrom-Json
        if (
            $document.schema -ne "yonerai.search-result.v1" -or
            $document.request_id -ne $requestId -or
            $document.query_digest -ne (ConvertTo-CanonicalQueryDigest -Query $query) -or
            $document.backend_ids.Count -ne 1 -or
            $document.backend_ids[0] -ne "searxng.local" -or
            $document.evidence.Count -lt 1
        ) {
            throw "Search Gateway live JSON result contract is invalid."
        }
    }
    finally {
        $response.Dispose()
        $content.Dispose()
    }

    [pscustomobject]@{
        schema = "yonerai.search-sandbox.owner-smoke.v1"
        ready = $true
        backend_id = "searxng.local"
        result_count = $document.evidence.Count
        vendor_fee_class = "zero_per_query"
        paid_fallback_used = $false
        openai_web_search_calls = 0
        configured_raw_query_retention_seconds = 0
        gateway_persistent_query_store = $false
        upstream_query_log_retention = "unverified"
    } | ConvertTo-Json -Compress
}
finally {
    $client.Dispose()
    $handler.Dispose()
}
