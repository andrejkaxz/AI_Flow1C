[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$InstallOfficeTools,
    [switch]$InstallDevTools,
    [switch]$InstallCc1cSkills,
    [switch]$InstallBslLanguageServer,
    [switch]$InstallPrerequisites,
    [switch]$MinimalDependencies,
    [string]$GitInstaller = "",
    [string]$PythonInstaller = "",
    [ValidateSet("conversation", "project-basic", "documents", "analysis", "implementation", "full", "template-markdown", "template-docx")][string]$Profile = "",
    [switch]$Plan,
    [switch]$Json,
    [string]$Wheelhouse = "",
    [switch]$NoIndex,
    [switch]$NoCacheDir,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Steps = New-Object System.Collections.Generic.List[object]
$script:CurrentStep = "bootstrap initialization"
function Add-Step([string]$Name, [string]$State, [string]$Detail) {
    $Steps.Add([ordered]@{ name = $Name; state = $State; detail = $Detail })
}
function Emit([string]$State, [bool]$Ready, [string]$NextAction = "") {
    $Result = [ordered]@{ schema_version = 1; state = $State; ready = $Ready; requested_profile = $(if ($Profile) { $Profile } else { "legacy" }); plan = [bool]$Plan; steps = $Steps; next_actions = @() }
    if ($NextAction) { $Result.next_actions = @($NextAction) }
    if ($Json) { $Result | ConvertTo-Json -Depth 8 } else { $Steps | ForEach-Object { Write-Host "$($_.state): $($_.name) - $($_.detail)" } }
}
function Get-SupportedPythonVersion([string]$Executable, [string[]]$Arguments = @()) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    try { $Value = (& $Executable @Arguments -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null | Out-String).Trim() }
    catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $Value) { return $null }
    try { $Version = [version]$Value } catch { return $null }
    if ($Version -lt [version]"3.10") { return $null }
    return $Version
}

trap {
    $FailureMessage = [string]$_.Exception.Message
    if (-not $FailureMessage -and $Error.Count -gt 0) { $FailureMessage = [string]$Error[0].Exception.Message }
    if (-not $FailureMessage) {
        $FailedComponent = if ($Steps.Count -gt 0) { [string]$Steps[$Steps.Count - 1].name } else { $script:CurrentStep }
        $FailureMessage = "$FailedComponent failed without child-process details; rerun with -Resume without -Json. Existing .venv and local configuration were preserved."
    }
    $Failure = [ordered]@{
        schema_version = 1
        state = "BLOCKED"
        ready = $false
        requested_profile = $(if ($Profile) { $Profile } else { "legacy" })
        steps = $Steps
        errors = @([ordered]@{
            code = "BOOTSTRAP_FAILED"
            message = $FailureMessage
            recoverable = $true
            next_action = "Correct the reported prerequisite or input and rerun bootstrap with the same profile."
        })
        next_actions = @("bootstrap.ps1 -Profile $EffectiveProfile -Resume")
    }
    if ($Json) { $Failure | ConvertTo-Json -Depth 8 } else { [Console]::Error.WriteLine($FailureMessage) }
    exit 1
}

$EffectiveProfile = if ($Profile) { $Profile } elseif ($MinimalDependencies) { "conversation" } elseif ($InstallDevTools) { "implementation" } elseif ($InstallOfficeTools) { "documents" } else { "analysis" }
$NeedOffice = $EffectiveProfile -in @("documents", "analysis", "implementation", "full")
$NeedRlm = $EffectiveProfile -in @("analysis", "implementation", "full")
$NeedBsl = $InstallBslLanguageServer -or $EffectiveProfile -in @("implementation", "full")
$NeedSkills = $InstallCc1cSkills -or $EffectiveProfile -eq "full"
$VenvPath = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$ExistingVenvVersion = Get-SupportedPythonVersion $VenvPython

if ($ExistingVenvVersion) {
    Add-Step "Virtual environment" "PLANNED" "reuse compatible .venv Python $ExistingVenvVersion"
} elseif (Test-Path -LiteralPath $VenvPath) {
    Add-Step "Virtual environment" "PLANNED" "preserve incompatible .venv in .workspace/backups and recreate it with Python >=3.10"
} else {
    Add-Step "Create virtual environment" "PLANNED" ".venv using supported Python >=3.10"
}
if ($NeedOffice) { Add-Step "Install document dependencies" "PLANNED" "requirements-capabilities/documents.txt" }
if ($EffectiveProfile -in @("template-markdown", "template-docx")) { Add-Step "Install template dependencies" "PLANNED" "requirements-capabilities/$EffectiveProfile.txt (no Git, RLM or Office application)" }
if ($NeedRlm) { Add-Step "Install RLM" "PLANNED" "requirements-capabilities/rlm.txt and start compatible service" }
if ($NeedBsl) { Add-Step "Install BSL Language Server" "PLANNED" "verified manifest version and SHA-256" }
if ($NeedSkills) { Add-Step "Install cc-1c-skills" "PLANNED" "explicit opt-in external checkout" }
if ($Wheelhouse) { Add-Step "Offline wheelhouse" "PLANNED" $Wheelhouse }
if ($InstallPrerequisites) { Add-Step "Install prerequisites" "PLANNED" "requires explicit invocation; no hidden elevation" }

if ($EffectiveProfile -eq "full" -and -not $InstallCc1cSkills) {
    Add-Step "cc-1c-skills confirmation" "NEEDS_CONFIRMATION" "rerun with -InstallCc1cSkills after approving the external checkout"
    Emit "NEEDS_CONFIRMATION" $false "bootstrap.ps1 -Profile full -InstallCc1cSkills"
    exit 1
}
if ($Plan) { Emit "PLAN" $false; exit 0 }

if ($InstallPrerequisites) {
    if ($Json) { & (Join-Path $PSScriptRoot "install-prerequisites.ps1") -GitInstaller $GitInstaller -PythonInstaller $PythonInstaller *> $null }
    else { & (Join-Path $PSScriptRoot "install-prerequisites.ps1") -GitInstaller $GitInstaller -PythonInstaller $PythonInstaller }
}

$PythonArgs = @()
$PythonVersion = Get-SupportedPythonVersion $VenvPython
if (-not $PythonVersion) {
    if ($Python) {
        $PythonVersion = Get-SupportedPythonVersion $Python
        if (-not $PythonVersion) { throw "Python >=3.10 is required; the explicitly selected interpreter is incompatible." }
    } else {
        $Launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($Launcher) {
            foreach ($Candidate in @("3", "3.14", "3.13", "3.12", "3.11", "3.10")) {
                $CandidateArgs = @("-$Candidate")
                $CandidateVersion = Get-SupportedPythonVersion $Launcher.Source $CandidateArgs
                if ($CandidateVersion) { $Python = $Launcher.Source; $PythonArgs = $CandidateArgs; $PythonVersion = $CandidateVersion; break }
            }
        }
        if (-not $Python) {
            $Command = Get-Command python -ErrorAction SilentlyContinue
            if ($Command -and $Command.Source -notlike "*\WindowsApps\python.exe") {
                $CandidateVersion = Get-SupportedPythonVersion $Command.Source
                if ($CandidateVersion) { $Python = $Command.Source; $PythonVersion = $CandidateVersion }
            }
        }
    }
    if (-not $Python -or -not $PythonVersion) { throw "Supported Python >=3.10 not found." }
    if (Test-Path -LiteralPath $VenvPath) {
        $RootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
        $VenvFull = [IO.Path]::GetFullPath($VenvPath)
        if (-not $VenvFull.StartsWith("$RootFull$([IO.Path]::DirectorySeparatorChar)", [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to move a virtual environment outside the workflow root: $VenvFull"
        }
        $BackupRoot = Join-Path $Root ".workspace\backups"
        $null = New-Item -ItemType Directory -Force -Path $BackupRoot
        $VenvBackup = Join-Path $BackupRoot ("venv-repair-" + [DateTimeOffset]::Now.ToString("yyyyMMdd-HHmmss"))
        Move-Item -LiteralPath $VenvPath -Destination $VenvBackup
        Add-Step "Virtual environment backup" "COMPLETE" $VenvBackup
    }
    if ($Json) { & $Python @PythonArgs -m venv $VenvPath *> $null } else { & $Python @PythonArgs -m venv $VenvPath }
    if ($LASTEXITCODE -ne 0) { throw "Cannot create .venv with Python $PythonVersion." }
    $CreatedVersion = Get-SupportedPythonVersion $VenvPython
    if (-not $CreatedVersion) { throw "The recreated .venv does not contain a supported Python." }
    $PythonVersion = $CreatedVersion
}
$PipArgs = @("-m", "pip", "install")
if ($NoCacheDir) { $PipArgs += "--no-cache-dir" }
if ($Wheelhouse) {
    if (-not [IO.Path]::IsPathRooted($Wheelhouse)) { throw "Wheelhouse must be an absolute path." }
    $PipArgs += @("--no-index", "--find-links", $Wheelhouse)
} elseif ($NoIndex) { $PipArgs += "--no-index" }
$Requirements = ""
if ($InstallDevTools) { $Requirements = "requirements-dev.txt" }
elseif ($EffectiveProfile -in @("template-markdown", "template-docx")) { $Requirements = "requirements-capabilities\$EffectiveProfile.txt" }
elseif (-not $Profile -and $MinimalDependencies -and -not $InstallOfficeTools) { $Requirements = "requirements.txt" }
elseif ($NeedRlm) { $Requirements = "requirements-capabilities\rlm.txt" }
elseif ($NeedOffice) { $Requirements = "requirements-capabilities\documents.txt" }
if ($Requirements) {
    if ($PythonVersion.Major -eq 3 -and $PythonVersion.Minor -eq 12) {
        $PipArgs += @("-c", "requirements-capabilities\constraints-py312.txt")
    }
    $PipLog = ""
    if ($Json) {
        $PipLogRoot = Join-Path $Root ".workspace\diagnostics"
        $null = New-Item -ItemType Directory -Force -Path $PipLogRoot
        $PipLog = Join-Path $PipLogRoot ("bootstrap-pip-" + [Guid]::NewGuid().ToString() + ".log")
        $PreviousErrorPreference = $ErrorActionPreference
        try {
            # Native stderr (e.g. pip notices) must not abort a successful JSON run.
            # Keep diagnostics locally and use the native process exit status.
            $ErrorActionPreference = "Continue"
            & $VenvPython @PipArgs -r $Requirements *> $PipLog
            $PipExitCode = $LASTEXITCODE
        } finally { $ErrorActionPreference = $PreviousErrorPreference }
    } else {
        & $VenvPython @PipArgs -r $Requirements
        $PipExitCode = $LASTEXITCODE
    }
    if ($PipExitCode -ne 0) { throw "Cannot install dependencies from $Requirements (exit $PipExitCode). Diagnostics: $PipLog" }
}
if (-not (Test-Path -LiteralPath ".flow1c.json")) { Copy-Item -LiteralPath "config\workflow.example.json" -Destination ".flow1c.json" }
if ($NeedBsl) {
    $script:CurrentStep = "BSL Language Server installation"
    if ($Json) {
        $BslOutput = ""
        try { $BslOutput = (& (Join-Path $PSScriptRoot "install-bsl-language-server.ps1") 2>&1 | Out-String).Trim() }
        catch {
            $BslDetail = [string]$_.Exception.Message
            if (-not $BslDetail) { $BslDetail = $BslOutput }
            if (-not $BslDetail) { $BslDetail = "installer stopped without details" }
            throw "BSL Language Server installation failed: $BslDetail"
        }
    }
    else { & (Join-Path $PSScriptRoot "install-bsl-language-server.ps1") }
    if ($LASTEXITCODE -ne 0) { throw "BSL Language Server installation failed with exit code $LASTEXITCODE; rerun bootstrap with -Resume without -Json for download details." }
}
if ($NeedRlm) {
    $script:CurrentStep = "RLM startup"
    if ($Json) {
        $RlmOutput = ""
        try { $RlmOutput = (& (Join-Path $PSScriptRoot "start-rlm-tools-bsl.ps1") 2>&1 | Out-String).Trim() }
        catch {
            $RlmDetail = [string]$_.Exception.Message
            if (-not $RlmDetail) { $RlmDetail = $RlmOutput }
            if (-not $RlmDetail) { $RlmDetail = "service launcher stopped without details" }
            throw "RLM startup failed: $RlmDetail"
        }
    }
    else { & (Join-Path $PSScriptRoot "start-rlm-tools-bsl.ps1") }
    if ($LASTEXITCODE -ne 0) { throw "RLM startup failed with exit code $LASTEXITCODE; rerun bootstrap with -Resume without -Json for service details." }
}

if ($NeedSkills) {
    $ExternalManifest = Get-Content -LiteralPath (Join-Path $Root "config\external-tools.json") -Raw -Encoding utf8 | ConvertFrom-Json
    $SkillsConfig = $ExternalManifest.cc_1c_skills
    $SkillsRepo = Join-Path $Root ([string]$SkillsConfig.path)
    if (Test-Path -LiteralPath (Join-Path $SkillsRepo ".git")) {
        $Dirty = ([string](git -C $SkillsRepo -c core.excludesFile= status --porcelain | Out-String)).Trim()
        if ($Dirty) { throw "cc-1c-skills contains local changes; bootstrap will not overwrite them." }
        git -C $SkillsRepo fetch ([string]$SkillsConfig.remote) ([string]$SkillsConfig.ref)
        if ($LASTEXITCODE -ne 0) { throw "Cannot fetch cc-1c-skills." }
        git -C $SkillsRepo merge --ff-only "$([string]$SkillsConfig.remote)/$([string]$SkillsConfig.ref)"
        if ($LASTEXITCODE -ne 0) { throw "cc-1c-skills has diverged; automatic update is blocked." }
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SkillsRepo) | Out-Null
        git clone --single-branch --branch ([string]$SkillsConfig.ref) ([string]$SkillsConfig.repository) $SkillsRepo
        if ($LASTEXITCODE -ne 0) { throw "Cannot clone cc-1c-skills." }
    }
    if ($Json) { & $VenvPython (Join-Path $PSScriptRoot "external_tools.py") --mode connect-skills --offline *> $null }
    else { & $VenvPython (Join-Path $PSScriptRoot "external_tools.py") --mode connect-skills --offline }
    if ($LASTEXITCODE -ne 0) { throw "Cannot connect cc-1c-skills to the project." }
}
Add-Step "Bootstrap" "COMPLETE" "Profile $EffectiveProfile is installed"
Emit "COMPLETE" $true
