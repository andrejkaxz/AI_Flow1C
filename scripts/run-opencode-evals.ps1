[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Model,
    [string]$OpenCodePath = "",
    [string]$PythonPath = "",
    [int]$Runs = 20,
    [string]$Scenario = "",
    [int]$TimeoutSeconds = 180,
    [string]$BaselineRef = "",
    [switch]$FixtureOnly,
    [string]$Variant = "",
    [string]$OutputDirectory = "",
    [switch]$Routing,
    [ValidateSet("all", "development", "held-out")][string]$Split = "all"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) {
    $PythonPath = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        $PythonPath = (Get-Command python -ErrorAction Stop).Source
    }
}
if (-not $OpenCodePath -and -not $FixtureOnly) {
    $OpenCodeCommand = Get-Command opencode -ErrorAction Stop
    $OpenCodePath = $OpenCodeCommand.Source
    if ($OpenCodePath.EndsWith(".ps1", [System.StringComparison]::OrdinalIgnoreCase)) {
        $NpmRoot = Split-Path -Parent $OpenCodePath
        $NativeOpenCode = Join-Path $NpmRoot "node_modules\opencode-ai\bin\opencode.exe"
        if (Test-Path -LiteralPath $NativeOpenCode) {
            $OpenCodePath = $NativeOpenCode
        } else {
            throw "The opencode PowerShell shim was found, but its native executable is unavailable: $NativeOpenCode"
        }
    }
}
$Evaluator = "opencode_evals.py"
if ($Routing) {
    if (-not $OutputDirectory) { throw "Routing evals require a new -OutputDirectory outside the product checkout" }
    $Evaluator = "routing_evals.py"
}
$Arguments = @("-B", (Join-Path $PSScriptRoot $Evaluator), "--model", $Model, "--runs", "$Runs", "--timeout", "$TimeoutSeconds")
if ($OpenCodePath) { $Arguments += @("--opencode", $OpenCodePath) }
if ($Scenario) {
    if ($Routing) { $Arguments += @("--case", $Scenario) }
    else { $Arguments += @("--scenario", $Scenario) }
}
if ($Routing) { $Arguments += @("--split", $Split) }
if ($Variant) { $Arguments += @("--variant", $Variant) }
if ($OutputDirectory) { $Arguments += @("--output-dir", $OutputDirectory) }
if ($BaselineRef) { $Arguments += @("--baseline-ref", $BaselineRef) }
if ($FixtureOnly) { $Arguments += "--fixture-only" }
& $PythonPath @Arguments
exit $LASTEXITCODE
