[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Sha256 = "",
    [ValidateRange(10, 900)][int]$DownloadTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ManifestPath = Join-Path $Root "config\external-tools.json"
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($Manifest.schema_version -ne 1) { throw "Unsupported external tools manifest schema: $($Manifest.schema_version)" }
if (-not $Version) { $Version = [string]$Manifest.bsl_language_server.version }
if (-not $Sha256) { $Sha256 = [string]$Manifest.bsl_language_server.sha256 }
$DownloadUrl = [string]$Manifest.bsl_language_server.url
if ($Version -ne [string]$Manifest.bsl_language_server.version) {
    throw "Ad-hoc BSL versions are not allowed; update config\external-tools.json first."
}
$DownloadDirectory = Join-Path $Root ".tools\downloads"
$InstallDirectory = Join-Path $Root ".tools\bsl-language-server-v$Version"
$ArchivePath = Join-Path $DownloadDirectory "bsl-language-server_win-v$Version.zip"
$ExecutablePath = Join-Path $InstallDirectory "bsl-language-server\bsl-language-server.exe"
$LocalConfigPath = Join-Path $Root ".flow1c.local.json"

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    $null = New-Item -ItemType Directory -Force -Path $DownloadDirectory
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
        Write-Host "Downloading BSL Language Server $Version..."
        try {
            Invoke-WebRequest -Uri $DownloadUrl -OutFile $ArchivePath -TimeoutSec $DownloadTimeoutSeconds
        }
        catch {
            if (Test-Path -LiteralPath $ArchivePath) { Remove-Item -LiteralPath $ArchivePath -Force }
            throw "Cannot download BSL Language Server $Version within ${DownloadTimeoutSeconds}s: $($_.Exception.Message)"
        }
    }

    $ActualHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Sha256 -and $ActualHash -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -LiteralPath $ArchivePath -Force
        throw "Checksum mismatch for $ArchivePath. Expected $Sha256, got $ActualHash."
    }

    $null = New-Item -ItemType Directory -Force -Path $InstallDirectory
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $InstallDirectory
}

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "Installation finished without the expected executable: $ExecutablePath"
}
if (-not (Test-Path -LiteralPath $LocalConfigPath -PathType Leaf)) {
    Set-Content -LiteralPath $LocalConfigPath -Value "{}" -Encoding utf8
}

$LocalConfig = Get-Content -LiteralPath $LocalConfigPath -Raw -Encoding utf8 | ConvertFrom-Json
$Settings = [pscustomobject]@{
    command = $ExecutablePath
    version = $Version
    max_heap = "4g"
}
if ($LocalConfig.PSObject.Properties.Name -contains "bsl_language_server") {
    $LocalConfig.bsl_language_server = $Settings
}
else {
    $LocalConfig | Add-Member -NotePropertyName "bsl_language_server" -NotePropertyValue $Settings
}
$LocalConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $LocalConfigPath -Encoding utf8

Write-Host "Installed: $ExecutablePath"
Write-Host "Local configuration updated: $LocalConfigPath"
