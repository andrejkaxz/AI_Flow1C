[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("Ensure", "Status", "Wait")][string]$Action,
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [ValidateRange(0, 540)][int]$WaitSeconds = 480,
    [switch]$ForceUpdate,
    [switch]$RetryFailed,
    [string]$RequestId = "",
    [switch]$Json
)

# Policy, worker lifecycle and validation are shared with Python doctor.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Result = @{ schema_version = 2; state = "FAILED"; ready = $false; source_path = $SourcePath; detail = "Project Python is missing. Repair prerequisites before indexing." }
    $Result | ConvertTo-Json -Depth 6
    exit 1
}
$Arguments = @((Join-Path $Root "scripts\rlm_index_runtime.py"), $Action, "--root", $Root, "--source", $SourcePath, "--wait-seconds", $WaitSeconds)
if ($ForceUpdate) { $Arguments += "--force" }
if ($RetryFailed) { $Arguments += "--retry-failed" }
if ($RequestId) { $Arguments += @("--request-id", $RequestId) }
$Output = (& $Python @Arguments | Out-String).Trim()
$IndexExitCode = $LASTEXITCODE
if ($Json) { Write-Output $Output }
else {
    $Result = $Output | ConvertFrom-Json
    Write-Host "RLM index: $($Result.state)"
    Write-Host "Source: $($Result.source_path)"
    Write-Host $Result.detail
    if ($Result.stdout_log) { Write-Host "Log: $($Result.stdout_log)" }
}
exit $IndexExitCode
