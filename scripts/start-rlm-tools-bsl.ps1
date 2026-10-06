[CmdletBinding()]
param(
    [string]$Endpoint = "http://127.0.0.1:9000/mcp"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RlmCommand = Join-Path $Root ".venv\Scripts\rlm-tools-bsl.exe"

if (-not (Test-Path -LiteralPath $RlmCommand -PathType Leaf)) {
    throw "The requested RLM capability is not installed in .venv. Run bootstrap with profile analysis or higher."
}

try {
    $EndpointUri = [Uri]$Endpoint
}
catch {
    throw "Invalid RLM MCP endpoint: $Endpoint"
}
if ($EndpointUri.Scheme -ne "http" -or
    $EndpointUri.Host -notin @("127.0.0.1", "localhost", "::1") -or
    $EndpointUri.AbsolutePath.TrimEnd("/") -ne "/mcp") {
    throw "The bundled RLM launcher only accepts a loopback HTTP /mcp endpoint: $Endpoint"
}

$Port = $EndpointUri.Port
$HealthUrl = "http://$($EndpointUri.Authority)/health"
$RuntimeDirectory = Join-Path $Root ".workspace\rlm"
$PidPath = Join-Path $RuntimeDirectory "server.pid"
$EndpointHealthy = $false
function Test-CompatibleEndpoint {
    try {
        $Body = @{ jsonrpc = "2.0"; id = 1; method = "tools/list"; params = @{} } | ConvertTo-Json -Depth 4
        $Response = Invoke-WebRequest -Uri $Endpoint -Method POST -ContentType "application/json" -Headers @{ Accept = "application/json, text/event-stream" } -Body $Body -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        $Text = [string]$Response.Content
        return ($Text -match '"rlm_index"' -and $Text -match '"rlm_start"' -and $Text -match '"rlm_end"')
    }
    catch { return $false }
}
try {
    $null = Invoke-WebRequest -Uri $HealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
    $EndpointHealthy = $true
}
catch {
    if ($null -ne $_.Exception.Response) {
        $StatusCode = [int]$_.Exception.Response.StatusCode
        throw "RLM health endpoint returned HTTP $StatusCode at $HealthUrl."
    }
}

if ($EndpointHealthy) {
    $OwnedProcess = $null
    if (Test-Path -LiteralPath $PidPath -PathType Leaf) {
        $RecordedPid = 0
        if ([int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$RecordedPid)) {
            $OwnedProcess = Get-Process -Id $RecordedPid -ErrorAction SilentlyContinue
        }
    }
    if ($null -eq $OwnedProcess) {
        if (Test-CompatibleEndpoint) {
            Write-Host "Compatible shared RLM MCP is already running: $Endpoint"
            exit 0
        }
        throw "RLM endpoint $Endpoint is occupied by an unknown or incompatible process."
    }
    try {
        $ActualExecutable = [IO.Path]::GetFullPath($OwnedProcess.Path)
        $ExpectedExecutable = [IO.Path]::GetFullPath($RlmCommand)
    }
    catch {
        throw "RLM endpoint $Endpoint is healthy, but its process identity cannot be verified for this checkout."
    }
    if (-not $ActualExecutable.Equals($ExpectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        throw "RLM endpoint $Endpoint is owned by '$ActualExecutable', not this checkout's '$ExpectedExecutable'."
    }
    Write-Host "RLM MCP is already running: $Endpoint"
    exit 0
}

$null = New-Item -ItemType Directory -Force -Path $RuntimeDirectory
$StdoutPath = Join-Path $RuntimeDirectory "server.stdout.log"
$StderrPath = Join-Path $RuntimeDirectory "server.stderr.log"
$Process = Start-Process -FilePath $RlmCommand -ArgumentList @("--transport", "streamable-http", "--host", $EndpointUri.Host, "--port", $Port) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath -PassThru
Set-Content -LiteralPath $PidPath -Value $Process.Id -Encoding ascii

for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
    Start-Sleep -Seconds 2
    if ($Process.HasExited) {
        throw "RLM MCP exited during startup. See $StderrPath"
    }
    try {
        $null = Invoke-WebRequest -Uri $HealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        Write-Host "RLM MCP started: $Endpoint"
        exit 0
    }
    catch {
        # Keep waiting for a successful /health response.
    }
}

try { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue } catch {}
throw "RLM MCP did not become healthy at $HealthUrl. See $StderrPath"
