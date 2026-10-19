#requires -Version 7.5

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputsPath,
    [Parameter(Mandatory)][string]$AppConfigPath,
    [Parameter(Mandatory)][string]$ServiceUrl,
    [Parameter(Mandatory)][string]$HorizonStart,
    [Parameter(Mandatory)][string]$HorizonEnd,
    [ValidateSet('Prepare', 'Check')][string]$Mode = 'Prepare'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$deployment = & (Join-Path $PSScriptRoot 'deploy-mcp.ps1') -InputsPath $InputsPath -AppConfigPath $AppConfigPath -Mode Prepare
function Assert-McpBaseUrl([string]$Url) {


    if ($Url.Length -gt 261 -or $Url -cnotmatch '^https://(?<mcp_host>[A-Za-z0-9.-]+)\z') {
        throw 'mcp_base_service_url_required'
    }
    $mcpHostName = $Matches['mcp_host']
    if ($mcpHostName.Length -gt 253 -or -not $mcpHostName.EndsWith('.run.app', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'mcp_base_service_url_required'
    }
    foreach ($label in $mcpHostName.Split('.')) {
        if ($label.Length -lt 1 -or $label.Length -gt 63 -or
            $label -cnotmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\z') {
            throw 'mcp_base_service_url_required'
        }
    }
}
Assert-McpBaseUrl $ServiceUrl
function Convert-McpInstant($Value) {


    if ($Value -is [DateTimeOffset]) { return $Value }
    if ($Value -is [DateTime]) {
        if ($Value.Kind -ne [DateTimeKind]::Utc) { throw 'mcp_timezone_aware_horizon_required' }
        return [DateTimeOffset]$Value
    }
    if ($Value -isnot [string]) { throw 'mcp_horizon_invalid' }
    if ($Value -notmatch '(Z|[+-][0-9]{2}:[0-9]{2})$') { throw 'mcp_timezone_aware_horizon_required' }
    $instant = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse($Value, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None, [ref]$instant)) { throw 'mcp_horizon_invalid' }
    return $instant
}
$start = Convert-McpInstant $HorizonStart
$end = Convert-McpInstant $HorizonEnd
if ($end -le $start -or ($end - $start).TotalHours -gt 72) { throw 'mcp_horizon_must_be_positive_at_most_72_hours' }
$horizon = @{start = $start.ToString('o'); end = $end.ToString('o')}

function Invoke-McpWire {
    param([string]$BaseUrl, [string]$Token, $Body, [string]$Method = 'Post', [string]$Path = '/mcp',
        [string]$ProtocolVersion = '')
    Assert-McpBaseUrl $BaseUrl
    if ($Path -notin @('/mcp', '/healthz') -or $Method -notin @('Get', 'Post')) { throw 'mcp_wire_target_rejected' }
    $headers = @{Accept = 'application/json, text/event-stream'}
    if ($ProtocolVersion) {
        if ($ProtocolVersion -cne '2025-11-25') { throw 'mcp_protocol_version_unexpected' }
        $headers['MCP-Protocol-Version'] = $ProtocolVersion
    }
    if ($Token) { $headers['X-Serverless-Authorization'] = "Bearer $Token" }
    $parameters = @{Uri = $BaseUrl + $Path; Method = $Method; Headers = $headers;
        TimeoutSec = 30; MaximumRedirection = 0; MaximumRetryCount = 0; SkipHttpErrorCheck = $true; ErrorAction = 'Stop'}
    if ($Method -eq 'Post') { $parameters['ContentType'] = 'application/json'; $parameters['Body'] = $Body | ConvertTo-Json -Depth 15 -Compress }
    try {
        $response = Invoke-WebRequest @parameters

        if ([Text.Encoding]::UTF8.GetByteCount([string]$response.Content) -gt 2097152) { throw 'size' }
        $data = $null
        if ($response.Content -and [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300 -and
            (($Method -eq 'Post' -and $Body.Contains('id')) -or $Path -eq '/healthz')) {
            $contentType = [string]($response.Headers['Content-Type'] -join ',')
            if ($contentType -notmatch '^application/json(?:;|$)') { throw 'type' }

            $data = $response.Content | ConvertFrom-Json -AsHashtable -DateKind String
            if ($data -isnot [System.Collections.IDictionary]) { throw 'shape' }
        }
        return @{status = [int]$response.StatusCode; data = $data}
    } catch { throw 'mcp_http_failed_or_unexpected_response;outcome_unverified' }
    finally { $headers.Clear(); $Token = $null; $response = $null }
}
function Invoke-McpRpc([string]$BaseUrl, [string]$Token, [int]$Id, [string]$RpcMethod, $Parameters,
    [string]$ProtocolVersion = '') {
    $wire = Invoke-McpWire -BaseUrl $BaseUrl -Token $Token -ProtocolVersion $ProtocolVersion -Body @{jsonrpc = '2.0'; id = $Id; method = $RpcMethod; params = $Parameters}
    if ($wire.status -ne 200 -or $null -eq $wire.data -or $wire.data['jsonrpc'] -cne '2.0' -or
        $wire.data['id'] -ne $Id -or $wire.data.Contains('error') -or -not $wire.data.Contains('result')) {
        throw 'mcp_rpc_failed;outcome_unverified'
    }
    return $wire.data.result
}
function Invoke-McpReadSmokeCore {
    param([string]$BaseUrl, [string]$AuthorizedToken, [string]$WrongAudienceToken,
        [string]$UnauthorizedPrincipalToken, [Parameter(Mandatory)]$Window,
        [Parameter(Mandatory)]$ExpectedBinding)


    Assert-McpBaseUrl $BaseUrl
    if (-not $AuthorizedToken -or -not $WrongAudienceToken -or $AuthorizedToken -ceq $WrongAudienceToken) {
        throw 'mcp_distinct_positive_and_wrong_audience_tokens_required'
    }
    $s = Convert-McpInstant $Window.start; $e = Convert-McpInstant $Window.end
    if ($e -le $s -or ($e - $s).TotalHours -gt 72) { throw 'mcp_horizon_must_be_positive_at_most_72_hours' }
    foreach ($key in @('owner_id', 'run_id', 'config_revision', 'sheet_id', 'calendar_id')) {
        if ($ExpectedBinding -isnot [System.Collections.IDictionary] -or -not $ExpectedBinding.Contains($key) -or
            $ExpectedBinding[$key] -isnot [string] -or [string]::IsNullOrWhiteSpace($ExpectedBinding[$key])) {
            throw 'mcp_server_owned_expected_binding_required'
        }
    }
    $began = [DateTimeOffset]::UtcNow
    $probe = @{jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{protocolVersion = '2025-11-25'; capabilities = @{}; clientInfo = @{name = 'campuspulse-t12-read-smoke'; version = '0.1.0'}}}
    foreach ($negative in @('', $WrongAudienceToken)) {
        $wire = Invoke-McpWire -BaseUrl $BaseUrl -Token $negative -Body $probe
        if ($wire.status -notin @(401, 403)) { throw 'mcp_negative_auth_failed;outcome_unverified' }
    }
    $unauthorized = 'DEFERRED:no_test_identity'
    if ($UnauthorizedPrincipalToken) {
        if ($UnauthorizedPrincipalToken -ceq $AuthorizedToken) { throw 'mcp_distinct_negative_identity_required' }
        $wire = Invoke-McpWire -BaseUrl $BaseUrl -Token $UnauthorizedPrincipalToken -Body $probe
        if ($wire.status -notin @(401, 403)) { throw 'mcp_negative_principal_failed;outcome_unverified' }
        $unauthorized = 'checked'
    }
    $health = Invoke-McpWire -BaseUrl $BaseUrl -Token $AuthorizedToken -Body @{} -Method Get -Path '/healthz'
    if ($health.status -ne 200 -or $null -eq $health.data -or $health.data['status'] -cne 'alive') {
        throw 'mcp_health_failed;outcome_unverified'
    }
    $initialized = Invoke-McpRpc $BaseUrl $AuthorizedToken 1 'initialize' $probe.params
    if ($initialized['protocolVersion'] -cne '2025-11-25' -or -not $initialized.Contains('capabilities') -or
        -not $initialized.capabilities.Contains('tools')) { throw 'mcp_initialize_contract_failed' }


    $negotiatedVersion = [string]$initialized.protocolVersion
    $notification = Invoke-McpWire -BaseUrl $BaseUrl -Token $AuthorizedToken -ProtocolVersion $negotiatedVersion -Body @{jsonrpc = '2.0'; method = 'notifications/initialized'}
    if ($notification.status -ne 202) { throw 'mcp_initialized_notification_failed' }
    $listed = Invoke-McpRpc $BaseUrl $AuthorizedToken 2 'tools/list' @{} -ProtocolVersion $negotiatedVersion
    $names = @($listed.tools | ForEach-Object { $_.name } | Sort-Object)
    $expected = @('apply_plan', 'read_assignments', 'read_calendar', 'verify_plan')
    if ($names.Count -ne 4 -or (Compare-Object $names $expected) -or $listed.Contains('nextCursor')) {
        throw 'mcp_exact_full4_surface_required'
    }
    $summaries = @(); $id = 2
    foreach ($tool in @('read_assignments', 'read_calendar')) {
        $id++
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $result = Invoke-McpRpc $BaseUrl $AuthorizedToken $id 'tools/call' @{name = $tool; arguments = @{horizon = $Window}} -ProtocolVersion $negotiatedVersion
        $timer.Stop()
        if (($result.Contains('isError') -and ($result.isError -isnot [bool] -or $result.isError)) -or
            -not $result.Contains('structuredContent')) {
            throw 'mcp_read_tool_failed;outcome_unverified'
        }
        $snapshot = $result.structuredContent; $source = $snapshot.source; $reference = $source.reference
        $kind = if ($tool -eq 'read_assignments') {'assignments'} else {'calendar'}
        $resource = if ($tool -eq 'read_assignments') {$ExpectedBinding.sheet_id} else {$ExpectedBinding.calendar_id}
        if ($source.complete -isnot [bool] -or -not $source.complete -or
            $reference.kind -cne $kind -or $reference.resource_id -cne $resource -or
            $reference.owner_id -cne $ExpectedBinding.owner_id -or $reference.run_id -cne $ExpectedBinding.run_id -or
            $reference.config_revision -cne $ExpectedBinding.config_revision -or
            $reference.snapshot_id -cnotmatch '^[^\s\x00-\x1f]{1,256}$' -or
            $reference.content_hash -cnotmatch '^[0-9a-f]{64}$') { throw 'mcp_fresh_bound_complete_source_required' }
        $readAt = Convert-McpInstant $reference.read_at
        if ($readAt -lt $began.AddSeconds(-5) -or $readAt -gt [DateTimeOffset]::UtcNow.AddSeconds(5)) {
            throw 'mcp_source_not_fresh'
        }
        if ($kind -eq 'calendar' -and ((Convert-McpInstant $source.covered_interval.start) -ne $s -or
            (Convert-McpInstant $source.covered_interval.end) -ne $e -or
            $snapshot.recurrence_expanded -isnot [bool] -or -not $snapshot.recurrence_expanded)) {
            throw 'mcp_calendar_coverage_incomplete'
        }


        $summaries += @{tool = $tool; rpc_id = $id; elapsed_ms = $timer.ElapsedMilliseconds;
            read_at = $readAt.ToString('o'); content_hash = $reference.content_hash; complete = $true}
    }
    return @{status = 'wire_checks_passed'; evidence_scope = 'wire_response_validation_only';
        credentials_and_infrastructure_verified = $false; verified_at_utc = [DateTimeOffset]::UtcNow.ToString('o');
        anonymous = 'checked'; wrong_audience = 'checked'; unauthorized_principal = $unauthorized;
        surface = $names; reads = $summaries}
}
function Invoke-McpReadSmoke {
    param([string]$BaseUrl, [string]$AuthorizedToken, [string]$WrongAudienceToken,
        [string]$UnauthorizedPrincipalToken, [Parameter(Mandatory)]$Window,
        [Parameter(Mandatory)]$ExpectedBinding)
    try {
        Invoke-McpReadSmokeCore @PSBoundParameters
    } catch { throw 'mcp_read_smoke_failed;outcome_unverified' }
    finally { $AuthorizedToken = $null; $WrongAudienceToken = $null; $UnauthorizedPrincipalToken = $null }
}
if ($Mode -eq 'Check') {

    throw 'mcp_production_adapters_unimplemented:authenticator,run_binding,stopped_evidence;cloud_execution_deferred'
}
[ordered]@{status = 'OFFLINE_PREPARED'; executable = $false; network_class = 'none';
    service_url = $ServiceUrl; audience = $ServiceUrl; endpoint = "$ServiceUrl/mcp";
    horizon = $horizon; production_surface = $deployment.production_surface;
    calls = @('initialize', 'notifications/initialized', 'tools/list', 'read_assignments', 'read_calendar');
    negative_checks = @('anonymous', 'wrong_audience', 'unauthorized_principal_if_available');
    required_before_enable = @('trusted_run_binding_adapter', 'verified_audience_bound_token_provider',
        'actual_revision_image_service_identity_iam_readback', 'actual_workspace_sharing_and_scopes',
        'pinned_sdk_domain_result_validation');
    cloud_checks = 'DEFERRED'; health_startup = 'DEFERRED'}
