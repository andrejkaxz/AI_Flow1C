[CmdletBinding()]
param(
    [string]$GitInstaller = "",
    [string]$PythonInstaller = ""
)

$ErrorActionPreference = "Stop"

function Refresh-ProcessPath {
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

function Install-WithWinget([string]$PackageId) {
    & winget install --id $PackageId --exact --source winget --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet failed to install $PackageId (exit code $LASTEXITCODE)."
    }
}

function Find-SupportedPython {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        foreach ($Candidate in @("3", "3.14", "3.13", "3.12", "3.11", "3.10")) {
            $Value = & $Launcher.Source "-$Candidate" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $Value) {
                $Version = [version]$Value.Trim()
                if ($Version -ge [version]"3.10") { return $Launcher }
            }
        }
    }
    $Interpreter = Get-Command python -ErrorAction SilentlyContinue
    if ($Interpreter -and $Interpreter.Source -notlike "*\\WindowsApps\\python.exe") {
        $Value = & $Interpreter.Source -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $Value) {
            $Version = [version]$Value.Trim()
            if ($Version -ge [version]"3.10") { return $Interpreter }
        }
    }
    return $null
}

$NeedGit = -not (Get-Command git -ErrorAction SilentlyContinue)
$NeedPython = -not (Find-SupportedPython)

if (-not $NeedGit -and -not $NeedPython) {
    Write-Host "Git and Python are already installed."
    return
}

$Winget = Get-Command winget -ErrorAction SilentlyContinue
if ($NeedGit) {
    if ($GitInstaller) {
        if (-not (Test-Path -LiteralPath $GitInstaller -PathType Leaf)) {
            throw "Git installer not found: $GitInstaller"
        }
        $Process = Start-Process -FilePath $GitInstaller -ArgumentList "/VERYSILENT", "/NORESTART" -Wait -PassThru -WindowStyle Hidden
        if ($Process.ExitCode -ne 0) { throw "Git installer exited with code $($Process.ExitCode)." }
    }
    elseif ($Winget) {
        Install-WithWinget "Git.Git"
    }
    else {
        throw "Git is missing and WinGet is unavailable. Provide the approved Git for Windows installer with -GitInstaller."
    }
}

if ($NeedPython) {
    if ($PythonInstaller) {
        if (-not (Test-Path -LiteralPath $PythonInstaller -PathType Leaf)) {
            throw "Python installer not found: $PythonInstaller"
        }
        $Arguments = "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_test=0"
        $Process = Start-Process -FilePath $PythonInstaller -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
        if ($Process.ExitCode -ne 0) { throw "Python installer exited with code $($Process.ExitCode)." }
    }
    elseif ($Winget) {
        Install-WithWinget "Python.Python.3.14"
    }
    else {
        throw "Python is missing and WinGet is unavailable. Provide an approved Python 3.10 or newer installer with -PythonInstaller."
    }
}

Refresh-ProcessPath
$Git = Get-Command git -ErrorAction SilentlyContinue
$Python = Find-SupportedPython
if (-not $Git) { throw "Git installation completed, but git is not available in PATH. Start a new terminal and retry." }
if (-not $Python) { throw "Python installation completed, but Python is not available in PATH. Start a new terminal and retry." }

Write-Host "Git: $($Git.Source)"
Write-Host "Python: $($Python.Source)"
