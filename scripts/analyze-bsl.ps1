[CmdletBinding()]
param(
    [string]$SourcePath = "",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$WorkflowConfigPath = Join-Path $Root ".flow1c.json"
$LocalConfigPath = Join-Path $Root ".flow1c.local.json"

if (-not (Test-Path -LiteralPath $WorkflowConfigPath -PathType Leaf)) {
    throw "Missing $WorkflowConfigPath"
}
if (-not (Test-Path -LiteralPath $LocalConfigPath -PathType Leaf)) {
    throw "Missing $LocalConfigPath. Run bootstrap.ps1 and configure local paths."
}

$WorkflowConfig = Get-Content -LiteralPath $WorkflowConfigPath -Raw -Encoding utf8 | ConvertFrom-Json
$LocalConfig = Get-Content -LiteralPath $LocalConfigPath -Raw -Encoding utf8 | ConvertFrom-Json

if (-not $SourcePath) {
    $SourcePath = [string]$LocalConfig.extension_path
}
if (-not $SourcePath -or -not (Test-Path -LiteralPath $SourcePath -PathType Container)) {
    throw "1C source directory not found: $SourcePath"
}
$SourcePath = (Resolve-Path -LiteralPath $SourcePath).Path

$BslSettings = $LocalConfig.bsl_language_server
$Command = if ($BslSettings) { [string]$BslSettings.command } else { "" }
if (-not $Command -or -not (Test-Path -LiteralPath $Command -PathType Leaf)) {
    throw "BSL Language Server executable is not configured in .flow1c.local.json."
}
$Command = (Resolve-Path -LiteralPath $Command).Path

$ConfigRelative = [string]$WorkflowConfig.quality.bsl_language_server_config
if (-not $ConfigRelative) {
    $ConfigRelative = ".bsl-language-server.json"
}
$BslConfigPath = Join-Path $Root $ConfigRelative
if (-not (Test-Path -LiteralPath $BslConfigPath -PathType Leaf)) {
    throw "BSL Language Server configuration not found: $BslConfigPath"
}

if (-not $OutputPath) {
    $RunId = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $Root ".workspace\diagnostics\bsl-ls\$RunId"
}
$null = New-Item -ItemType Directory -Force -Path $OutputPath
$OutputPath = (Resolve-Path -LiteralPath $OutputPath).Path

$Reporters = @($WorkflowConfig.quality.bsl_language_server_reporters)
if ($Reporters.Count -eq 0) {
    $Reporters = @("json", "sarif")
}
$ReporterArguments = foreach ($Reporter in $Reporters) {
    "-r"
    [string]$Reporter
}

$MaxHeap = if ($BslSettings -and $BslSettings.max_heap) { [string]$BslSettings.max_heap } else { "4g" }
$PreviousJavaOptions = $env:JDK_JAVA_OPTIONS
$env:JDK_JAVA_OPTIONS = "-Xmx$MaxHeap"

Write-Host "BSL Language Server: $Command"
Write-Host "Source: $SourcePath"
Write-Host "Reports: $OutputPath"

try {
    & $Command analyze -c $BslConfigPath -s $SourcePath -w $SourcePath -o $OutputPath @ReporterArguments
    if ($LASTEXITCODE -ne 0) {
        throw "BSL Language Server exited with code $LASTEXITCODE."
    }
}
finally {
    if ($null -eq $PreviousJavaOptions) {
        Remove-Item Env:JDK_JAVA_OPTIONS -ErrorAction SilentlyContinue
    }
    else {
        $env:JDK_JAVA_OPTIONS = $PreviousJavaOptions
    }
}

Write-Host "Analysis complete."
Get-ChildItem -LiteralPath $OutputPath -File | ForEach-Object {
    Write-Host $_.FullName
}
