[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$CheckExternalToolsOnly,
    [switch]$SkipExternalToolUpdates,
    [switch]$AllowExternalUpdates,
    [switch]$RepairPrerequisites,
    [string]$UpdateId = "",
    [ValidateRange(0, 30)][int]$WaitSeconds = 0,
    [switch]$RetryFailedIndexes
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location -LiteralPath $Root
$Steps = New-Object 'System.Collections.Generic.List[object]'
$PreviousCommit = ""; $CurrentCommit = ""; $PreviousVersion = "unknown"; $CurrentVersion = "unknown"
$BackupDirectory = ""; $SettingsReconfirmationRequired = $false
$ExternalTools = [ordered]@{}; $Indexes = [ordered]@{ state = "NOT_CHECKED"; sources = @() }; $SmokeTests = [ordered]@{ state = "NOT_RUN" }
$NextActions = @()
$AgentRuntime = [ordered]@{ state = "NOT_CHECKED"; restart_required = $false; changed_files = @(); next_action = "" }
$ExtensionUpdate = [ordered]@{ state = "NOT_CHECKED"; previous_commit = ""; current_commit = "" }
$ResumeLoaded = $false; $ResumeSnapshot = $null; $ForcedSources = @(); $ExternalState = "CURRENT"
$RequestedResume = [bool]$UpdateId
$ResumeValidated = $false
$Phase = "preflight"; $CheckpointPath = ""; $ConfigSignature = ""
$UpdateLock = $null; $LockAcquired = $false
$UpdateOptions = [ordered]@{ allow_external_updates = [bool]$AllowExternalUpdates; skip_external_tool_updates = [bool]$SkipExternalToolUpdates; repair_prerequisites = [bool]$RepairPrerequisites }
if ($UpdateId -and $UpdateId -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
    $InvalidResult = @{ schema_version = 2; state = "BLOCKED"; ready = $false; error = "UpdateId must be a UUID from an update result; no files were changed." }
    if ($Json) { $InvalidResult | ConvertTo-Json } else { Write-Host $InvalidResult.error }
    exit 1
}
if (-not $UpdateId) { $UpdateId = [guid]::NewGuid().ToString() }
$CheckpointDirectory = Join-Path $Root ".workspace\updates"
$CheckpointPath = Join-Path $CheckpointDirectory "$UpdateId.json"

function Save-UpdateCheckpoint([object]$Result) {
    $null = New-Item -ItemType Directory -Force -Path $CheckpointDirectory
    $Temporary = Join-Path $CheckpointDirectory "$UpdateId.$([guid]::NewGuid()).tmp"
    $Destination = if (-not $LockAcquired -or ($RequestedResume -and -not $ResumeValidated)) { "$CheckpointPath.last-error.json" } else { $CheckpointPath }
    try {
        $Result | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $Temporary -Encoding utf8
        if (Test-Path -LiteralPath $Destination) { [IO.File]::Replace($Temporary, $Destination, "$Destination.previous") }
        else { [IO.File]::Move($Temporary, $Destination) }
    } finally { Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue }
}
function Get-ConfigSignature([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path); $Hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Hash.ComputeHash($Stream))).Replace("-", "") }
    finally { $Stream.Dispose(); $Hash.Dispose() }
}

function Add-Step([string]$Name, [string]$State, [string]$Detail) {
    if ($Steps.Count -ge 24) { $Steps.RemoveAt(0) }
    $Steps.Add([ordered]@{ name = $Name; state = $State; detail = $Detail; completed_at = [DateTimeOffset]::Now.ToString("o") })
    # A separate progress file remains readable while the final JSON is buffered.
    if (-not $CheckExternalToolsOnly -and $LockAcquired) {
        $null = New-Item -ItemType Directory -Force -Path $CheckpointDirectory
        @{ update_id = $UpdateId; phase = $Phase; step = $Name; state = $State; detail = $Detail; updated_at = [DateTimeOffset]::Now.ToString("o") } |
            ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $CheckpointDirectory "$UpdateId.progress.json") -Encoding utf8
        $Event = ($Steps[$Steps.Count - 1] | ConvertTo-Json -Depth 6 -Compress) + "`n"
        [IO.File]::AppendAllText((Join-Path $CheckpointDirectory "$UpdateId.events.jsonl"), $Event, [Text.UTF8Encoding]::new($false))
    }
}
function Invoke-Git([string[]]$Arguments, [string]$Repository = $Root) {
    $ErrorPath = [IO.Path]::GetTempFileName()
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 wraps any native stderr output in a
        # NativeCommandError. Git writes normal fetch progress to stderr, so
        # capture it and decide success exclusively from the native exit code.
        $ErrorActionPreference = "Continue"
        $Output = ([string](& git -C $Repository -c core.excludesFile= @Arguments 2> $ErrorPath | Out-String)).Trim(); $ExitCode = $LASTEXITCODE
        $ErrorActionPreference = $PreviousErrorActionPreference
        $ErrorOutput = Get-Content -LiteralPath $ErrorPath -Raw -ErrorAction SilentlyContinue
        if ($null -eq $ErrorOutput) { $ErrorOutput = "" } else { $ErrorOutput = ([string]$ErrorOutput).Trim() }
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
        Remove-Item -LiteralPath $ErrorPath -Force -ErrorAction SilentlyContinue
    }
    if ($ExitCode -ne 0) { $Detail = (@($Output, $ErrorOutput) | Where-Object { $_ }) -join "`n"; throw "git $($Arguments -join ' ') failed: $Detail" }
    return $Output
}
function Get-ProductVersion() {
    $Path = Join-Path $Root "VERSION"
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return (Get-Content -LiteralPath $Path -Raw -Encoding utf8).Trim() }
    return "unknown"
}
function Emit-Result([string]$State, [string]$ErrorMessage = "", [switch]$CheckpointOnly) {
    $Result = [ordered]@{
        schema_version = 2; state = $State; ready = ($State -eq "READY")
        previous_version = $PreviousVersion; current_version = $CurrentVersion
        previous_commit = $PreviousCommit; current_commit = $CurrentCommit; backup_directory = $BackupDirectory
        settings_reconfirmation_required = $SettingsReconfirmationRequired; external_tools = $ExternalTools
        indexes = $Indexes; extension_update = $ExtensionUpdate; smoke_tests = $SmokeTests; steps = $Steps; next_actions = $NextActions; agent_runtime = $AgentRuntime; error = $ErrorMessage
        update_id = $UpdateId; checkpoint_path = $CheckpointPath; phase = $Phase
        config_signature = $ConfigSignature; options = $UpdateOptions; forced_sources = @($ForcedSources)
        external_state = $ExternalState; jobs = @($Indexes.sources | Where-Object state -eq "RUNNING")
    }
    if (-not $CheckExternalToolsOnly) { Save-UpdateCheckpoint $Result }
    if ($CheckpointOnly) { return }
    if ($Json) {
        if ($State -eq "WAITING_BACKGROUND") { $Result.steps = @($Steps | Select-Object -Last 4) }
        $Result | ConvertTo-Json -Depth 12
    } else {
        Write-Host "Flow1C update: $State"
        foreach ($Step in $Steps) { Write-Host "$($Step.name): $($Step.state) - $($Step.detail)" }
        if ($ErrorMessage) { Write-Host "ERROR: $ErrorMessage" }
    }
}
function Get-SupportedPythonVersion([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    try { $Value = (& $Executable -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null | Out-String).Trim() }
    catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $Value) { return $null }
    try { $Version = [version]$Value } catch { return $null }
    if ($Version -lt [version]"3.10") { return $null }
    return $Version
}
function Invoke-ExternalTools([ValidateSet("check", "apply")][string]$Mode, [switch]$Offline) {
    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) { throw "The project virtual environment is missing. Run /setup." }
    $Arguments = @((Join-Path $Root "scripts\external_tools.py"), "--mode", $Mode, "--json")
    if ($Mode -eq "apply") { $Arguments += @("--backup-dir", $BackupDirectory) }
    if ($Offline) { $Arguments += "--offline" }
    $Output = (& $VenvPython @Arguments 2>&1 | Out-String).Trim(); $ExitCode = $LASTEXITCODE
    try { $Result = $Output | ConvertFrom-Json } catch { throw "External tools updater returned invalid JSON: $Output" }
    if ($ExitCode -ne 0 -or $Result.state -eq "BLOCKED") {
        $Detail = if ($Result.error) { [string]$Result.error } else { $Output }; throw "External tools $Mode failed: $Detail"
    }
    return $Result
}
function Stop-OwnedRlm {
    $PidPath = Join-Path $Root ".workspace\rlm\server.pid"
    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) { return }
    $RecordedPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$RecordedPid)) {
        $Process = Get-Process -Id $RecordedPid -ErrorAction SilentlyContinue
        if ($Process) {
            $Expected = [IO.Path]::GetFullPath((Join-Path $Root ".venv\Scripts\rlm-tools-bsl.exe"))
            if ([IO.Path]::GetFullPath($Process.Path).Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $RecordedPid -Force; Wait-Process -Id $RecordedPid -Timeout 15 -ErrorAction SilentlyContinue
            }
        }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}
function Update-Extension([object]$LocalConfig) {
    if ([string]$LocalConfig.extension_mode -ne "git") {
        return [ordered]@{ state = "SKIPPED"; previous_commit = ""; current_commit = ""; detail = "extension_mode is not git" }
    }
    $Path = [string]$LocalConfig.extension_path
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) { throw "Extension Git checkout is missing: $Path. Correct extension_path and rerun update." }
    $Repository = (Resolve-Path -LiteralPath $Path).Path
    $TopLevel = Invoke-Git @("rev-parse", "--show-toplevel") $Repository
    $Repository = (Resolve-Path -LiteralPath $TopLevel).Path
    $Dirty = Invoke-Git @("status", "--porcelain", "--untracked-files=no") $Repository
    if ($Dirty) { throw "Extension checkout has tracked local changes. Commit or revert them before updating.`n$Dirty" }
    $Branch = Invoke-Git @("symbolic-ref", "--quiet", "--short", "HEAD") $Repository
    if (-not $Branch) { throw "Extension checkout has detached HEAD. Switch to a tracking branch and rerun update." }
    $Remote = Invoke-Git @("config", "--get", "branch.$Branch.remote") $Repository
    if (-not $Remote -or $Remote -eq ".") { throw "Extension branch '$Branch' has no remote tracking repository." }
    $ExpectedUrl = [string]$LocalConfig.extension_repository_url
    $ActualUrl = Invoke-Git @("remote", "get-url", $Remote) $Repository
    . (Join-Path $PSScriptRoot "git-identity.ps1")
    if (-not $ExpectedUrl -or (Normalize-GitUrl $ActualUrl) -ne (Normalize-GitUrl $ExpectedUrl)) { throw "Extension remote URL does not match extension_repository_url. Use update-diagnose to compare the repository identities before choosing a repair." }
    $Upstream = Invoke-Git @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") $Repository
    $Before = Invoke-Git @("rev-parse", "HEAD") $Repository
    $null = Invoke-Git @("fetch", "--prune", $Remote) $Repository
    $Parts = (Invoke-Git @("rev-list", "--left-right", "--count", "HEAD...$Upstream") $Repository) -split "\s+"
    if ($Parts.Count -lt 2) { throw "Cannot parse extension Git divergence." }
    if ([int]$Parts[0] -gt 0) { throw "Extension branch '$Branch' contains commits not present in $Upstream. Resolve the divergence before updating." }
    if ([int]$Parts[1] -gt 0) { $null = Invoke-Git @("merge", "--ff-only", $Upstream) $Repository }
    $After = Invoke-Git @("rev-parse", "HEAD") $Repository
    return [ordered]@{ state = $(if ($After -ne $Before) { "UPDATED" } else { "CURRENT" }); previous_commit = $Before; current_commit = $After; detail = "$Branch tracks $Upstream" }
}
function Update-Indexes([object]$LocalConfig, [bool]$ExtensionChanged) {
    $Sources = @([string]$LocalConfig.configuration_path, [string]$LocalConfig.extension_path) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } | Select-Object -Unique
    $Results = @()
    $WaitDeadline = [DateTimeOffset]::Now.AddSeconds($WaitSeconds)
    foreach ($Source in $Sources) {
        $ForceUpdate = $ExtensionChanged -and ($Source -eq [string]$LocalConfig.extension_path)
        $Output = (& (Join-Path $Root "scripts\rlm-index.ps1") -Action Ensure -SourcePath $Source -ForceUpdate:$ForceUpdate -RequestId $UpdateId -RetryFailed:$RetryFailedIndexes -Json 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw "Cannot ensure RLM index for '$Source': $Output" }; $Status = $Output | ConvertFrom-Json
        if ($ForceUpdate) { $script:ForcedSources += $Source }
        $Remaining = [Math]::Max(0, [int]($WaitDeadline - [DateTimeOffset]::Now).TotalSeconds)
        if ($Status.state -eq "RUNNING" -and $Remaining -gt 0) {
            $Output = (& (Join-Path $Root "scripts\rlm-index.ps1") -Action Wait -SourcePath $Source -WaitSeconds $Remaining -Json 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) { throw "RLM index build failed for '$Source': $Output" }; $Status = $Output | ConvertFrom-Json
        }
        if ($Status.state -notin @("FRESH", "RUNNING")) { throw "RLM index for '$Source' is $($Status.state): $($Status.detail)" }
        $Results += $Status
        Add-Step "RLM index $Source" $Status.state $Status.detail
    }
    return [ordered]@{ state = $(if (@($Results | Where-Object state -eq "RUNNING").Count) { "RUNNING" } else { "CURRENT" }); sources = $Results }
}

try {
    if (-not $CheckExternalToolsOnly) {
        $null = New-Item -ItemType Directory -Force -Path $CheckpointDirectory
        try { $UpdateLock = [IO.File]::Open((Join-Path $CheckpointDirectory "update.lock"), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None); $LockAcquired = $true }
        catch { throw "Another update call is running in this checkout. Wait for it, then resume the same update_id; state and backups are preserved." }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git is not installed. Run /setup." }
    if (-not (Test-Path -LiteralPath (Join-Path $Root ".git"))) { throw "Flow1C is not a Git checkout." }
    $LocalConfigPath = Join-Path $Root ".flow1c.local.json"
    if (-not (Test-Path -LiteralPath $LocalConfigPath -PathType Leaf)) { $SettingsReconfirmationRequired = $true; throw "Local configuration is missing. Run /setup." }
    try { $LocalConfig = Get-Content -LiteralPath $LocalConfigPath -Raw -Encoding utf8 | ConvertFrom-Json }
    catch { $SettingsReconfirmationRequired = $true; throw "Local configuration is not valid JSON: $($_.Exception.Message)" }
    $ConfigSignature = Get-ConfigSignature $LocalConfigPath
    if ($RequestedResume -and -not (Test-Path -LiteralPath $CheckpointPath -PathType Leaf)) { throw "Update checkpoint does not exist. Start a new update without UpdateId." }
    if (Test-Path -LiteralPath $CheckpointPath) {
        $ResumeSnapshot = Get-Content -LiteralPath $CheckpointPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ($ResumeSnapshot.schema_version -ne 2 -or $ResumeSnapshot.update_id -ne $UpdateId) { throw "Unsupported or mismatched update checkpoint. Start a new update; existing files are preserved." }
        if ($ResumeSnapshot.config_signature -ne $ConfigSignature) { throw "Local configuration changed after this update began. Start a new update without UpdateId; the prior backup is preserved." }
        $Head = Invoke-Git @("rev-parse", "HEAD")
        if ($ResumeSnapshot.current_commit -and $ResumeSnapshot.current_commit -ne $Head) { throw "Workflow HEAD changed after this update began. Start a new update without UpdateId; the prior backup is preserved." }
        $ResumeValidated = $true
        $SameOptions = ($ResumeSnapshot.options.allow_external_updates -eq $UpdateOptions.allow_external_updates -and $ResumeSnapshot.options.skip_external_tool_updates -eq $UpdateOptions.skip_external_tool_updates -and $ResumeSnapshot.options.repair_prerequisites -eq $UpdateOptions.repair_prerequisites)
        if ($ResumeSnapshot.phase -in @("indexes", "doctor", "complete") -and $SameOptions) {
            foreach ($Name in @("PreviousCommit", "CurrentCommit", "PreviousVersion", "CurrentVersion", "BackupDirectory")) {
                $Property = @{ PreviousCommit = "previous_commit"; CurrentCommit = "current_commit"; PreviousVersion = "previous_version"; CurrentVersion = "current_version"; BackupDirectory = "backup_directory" }[$Name]
                Set-Variable -Name $Name -Value $ResumeSnapshot.$Property -Scope Script
            }
            $AgentRuntime = $ResumeSnapshot.agent_runtime; $ExternalTools = $ResumeSnapshot.external_tools; $ExternalState = $ResumeSnapshot.external_state
            $ExtensionUpdate = $ResumeSnapshot.extension_update; $ForcedSources = @($ResumeSnapshot.forced_sources)
            foreach ($Step in $ResumeSnapshot.steps) { $Steps.Add($Step) }
            $ResumeLoaded = $true
        }
    }
    Add-Step "Local configuration" "OK" "existing settings will be preserved"

    if ($CheckExternalToolsOnly) {
        $Check = Invoke-ExternalTools "check"; $ExternalTools = $Check.external_tools
        Add-Step "External tools check" $Check.state "read-only check completed"; Emit-Result $Check.state; exit 0
    }
    if (-not $ResumeLoaded) {
        $Phase = "workflow"
        $TrackedChanges = Invoke-Git @("status", "--porcelain", "--untracked-files=no")
        if ($TrackedChanges) { throw "Tracked Flow1C files have local changes. Commit or revert them before updating.`n$TrackedChanges" }
        Add-Step "Working tree" "OK" "no changes in tracked product files"
        $Branch = Invoke-Git @("symbolic-ref", "--quiet", "--short", "HEAD")
        if (-not $Branch) { throw "Detached HEAD is not supported for automatic updates." }
        $Remote = Invoke-Git @("config", "--get", "branch.$Branch.remote")
        if (-not $Remote -or $Remote -eq ".") { throw "Branch '$Branch' has no remote tracking repository." }
        $Upstream = Invoke-Git @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        $PreviousCommit = Invoke-Git @("rev-parse", "HEAD"); $CurrentCommit = $PreviousCommit
        if ($ResumeSnapshot -and $ResumeSnapshot.previous_commit) { $PreviousCommit = [string]$ResumeSnapshot.previous_commit }
        $CurrentVersion = Get-ProductVersion; $PreviousVersion = $CurrentVersion
        if ($ResumeSnapshot -and $ResumeSnapshot.previous_version) { $PreviousVersion = [string]$ResumeSnapshot.previous_version }
        Add-Step "Update source" "OK" "$Branch tracks $Upstream"
        if ($ResumeValidated -and $ResumeSnapshot.backup_directory) {
            $BackupDirectory = [IO.Path]::GetFullPath([string]$ResumeSnapshot.backup_directory)
            $BackupPrefix = [IO.Path]::GetFullPath((Join-Path $Root ".workspace\backups")) + [IO.Path]::DirectorySeparatorChar
            if (-not $BackupDirectory.StartsWith($BackupPrefix, [StringComparison]::OrdinalIgnoreCase) -or
                -not (Test-Path -LiteralPath (Join-Path $BackupDirectory ".flow1c.local.json") -PathType Leaf)) {
                throw "Saved update backup is missing or outside the backup directory. Preserve the checkpoint and repair the backup before resuming."
            }
            $BackupProbe = Join-Path $BackupDirectory ".flow1c.local.json"
            while ($BackupProbe) {
                if ((Get-Item -LiteralPath $BackupProbe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Saved update backup contains a symlink or junction."
                }
                $BackupParent = Split-Path -Parent $BackupProbe
                if (-not $BackupParent -or $BackupParent -eq $BackupProbe) { break }
                $BackupProbe = $BackupParent
            }
        } else {
            $Timestamp = [DateTimeOffset]::Now.ToString("yyyyMMdd-HHmmss-fff"); $BackupDirectory = Join-Path $Root ".workspace\backups\update-$Timestamp-$UpdateId"
            $null = New-Item -ItemType Directory -Force -Path $BackupDirectory
            Copy-Item -LiteralPath $LocalConfigPath -Destination (Join-Path $BackupDirectory ".flow1c.local.json")
            Set-Content -LiteralPath (Join-Path $BackupDirectory "previous-commit.txt") -Value $PreviousCommit -Encoding ascii
        }
        Add-Step "Backup" "OK" $BackupDirectory
        $null = Invoke-Git @("fetch", "--prune", "--tags", $Remote); Add-Step "Fetch" "OK" $Remote
        $Parts = (Invoke-Git @("rev-list", "--left-right", "--count", "HEAD...$Upstream")) -split "\s+"
        if ($Parts.Count -lt 2) { throw "Cannot parse Git divergence." }
        if ([int]$Parts[0] -gt 0) { throw "Local branch '$Branch' contains commits not present in $Upstream." }
        if ([int]$Parts[1] -gt 0) {
            $null = Invoke-Git @("merge", "--ff-only", $Upstream); $CurrentCommit = Invoke-Git @("rev-parse", "HEAD"); $CurrentVersion = Get-ProductVersion
            Add-Step "Workflow files" "UPDATED" "$PreviousCommit -> $CurrentCommit"
        } else { Add-Step "Workflow files" "CURRENT" $CurrentCommit }
        $AdapterChanges = @()
        if ($PreviousCommit -ne $CurrentCommit) {
            $AdapterDiff = Invoke-Git @("diff", "--name-only", $PreviousCommit, $CurrentCommit, "--", ".opencode", "opencode.json")
            $AdapterChanges = @($AdapterDiff -split "\r?\n" | Where-Object { $_ })
        }
        $AgentRuntime = [ordered]@{
            state = $(if ($AdapterChanges.Count) { "RESTART_REQUIRED" } else { "UNCHANGED" })
            restart_required = [bool]$AdapterChanges.Count; changed_files = $AdapterChanges
            next_action = $(if ($AdapterChanges.Count) { "Complete the successful update gate, fully quit and reopen OpenCode in the same project, then resume saved gate_id/setup_id/operation_id. Missing tools must not be replaced by flow1c_action or bash." } else { "" })
        }
        Add-Step "OpenCode runtime" $AgentRuntime.state $(if ($AdapterChanges.Count) { $AgentRuntime.next_action } else { "no adapter files changed in this update; active runtime was not inspected" })
        $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
        $VenvVersion = Get-SupportedPythonVersion $VenvPython
        if (-not $VenvVersion) {
            if (-not $RepairPrerequisites) {
                $RepairParameters = [ordered]@{ confirmed = $true; repair_prerequisites = $true }
                if ($AllowExternalUpdates) { $RepairParameters.allow_external_updates = $true }
                if ($SkipExternalToolUpdates) { $RepairParameters.skip_external_tool_updates = $true }
                $NextActions = @([ordered]@{ action = "update"; parameters = $RepairParameters })
                Add-Step "Python runtime" "NEEDS_CONFIRMATION" "install Python >=3.10 and safely recreate the project virtual environment"
                Emit-Result "NEEDS_CONFIRMATION" "A supported project Python is unavailable. Confirm prerequisite repair to continue this same update gate."
                exit 1
            }
            Stop-OwnedRlm
            $BootstrapOutput = (& (Join-Path $Root "scripts\bootstrap.ps1") -Profile analysis -InstallPrerequisites -Json 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) { throw "Prerequisite repair failed: $BootstrapOutput" }
            $VenvVersion = Get-SupportedPythonVersion $VenvPython
            if (-not $VenvVersion) { throw "Prerequisite repair completed without a supported project Python." }
            Add-Step "Python runtime" "REPAIRED" ".venv now uses Python $VenvVersion; the previous incompatible environment was preserved in .workspace/backups"
        } else {
            Add-Step "Python runtime" "OK" ".venv uses Python $VenvVersion"
        }
        $MigrationOutput = (& $VenvPython (Join-Path $Root "scripts\migrate-local-config.py") --path $LocalConfigPath --backup-dir $BackupDirectory --json 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { $SettingsReconfirmationRequired = $true; throw "Local configuration migration failed: $MigrationOutput" }
        $MigrationResult = $MigrationOutput | ConvertFrom-Json; Add-Step "Local configuration migration" ([string]$MigrationResult.state) "schema $($MigrationResult.current_schema_version)"
        $LocalConfig = Get-Content -LiteralPath $LocalConfigPath -Raw -Encoding utf8 | ConvertFrom-Json
        $ConfigSignature = Get-ConfigSignature $LocalConfigPath
        $ExtensionUpdate = Update-Extension $LocalConfig
        Add-Step "Extension repository" $ExtensionUpdate.state $ExtensionUpdate.detail

        $Apply = $false; $Check = $null
        if ($SkipExternalToolUpdates) {
            $ExternalTools = [ordered]@{ state = "SKIPPED"; detail = "emergency override requested" }; Add-Step "External tools check" "SKIPPED" "-SkipExternalToolUpdates was specified"
        } else {
            $Check = Invoke-ExternalTools "check"; $ExternalTools = $Check.external_tools; $ExternalState = $Check.state; Add-Step "External tools check" $Check.state "before snapshot included"
            $Manifest = Get-Content -LiteralPath (Join-Path $Root "config\external-tools.json") -Raw -Encoding utf8 | ConvertFrom-Json
            $Apply = $AllowExternalUpdates -or [bool]$Manifest.policy.apply_updates
            $DependencyFilesChanged = $false
            if ($PreviousCommit -ne $CurrentCommit) {
                $DependencyDiff = Invoke-Git @(
                    "diff", "--name-only", $PreviousCommit, $CurrentCommit, "--",
                    "requirements.txt", "requirements-office.txt", "requirements-dev.txt", "scripts/bootstrap.ps1"
                )
                $DependencyFilesChanged = [bool]$DependencyDiff
            }
            if ($DependencyFilesChanged -and $Check.external_tools.rlm_tools_bsl.state -eq "CURRENT") {
                $BootstrapOutput = (& (Join-Path $Root "scripts\bootstrap.ps1") 2>&1 | Out-String).Trim()
                if ($LASTEXITCODE -ne 0) { throw "Dependency synchronization failed: $BootstrapOutput" }
                Add-Step "Dependencies" "UPDATED" "bootstrap completed without changing the compatible RLM pin"
            } elseif ($DependencyFilesChanged) {
                Add-Step "Dependencies" "REVIEW_REQUIRED" "dependency files changed while RLM also needs action"
            } else {
                Add-Step "Dependencies" "CURRENT" "dependency manifests are unchanged"
            }
            if ($Check.state -eq "UPDATE_AVAILABLE" -and $Apply) {
                Stop-OwnedRlm
                try { $Applied = Invoke-ExternalTools "apply"; $ExternalTools = $Applied.external_tools; $ExternalState = $Applied.state }
                catch { try { & (Join-Path $Root "scripts\start-rlm-tools-bsl.ps1") | Out-Null } catch {}; throw }
                Add-Step "External tools update" "UPDATED" "allowed candidates were applied"
            } elseif ($Check.state -eq "UPDATE_AVAILABLE") { Add-Step "External tools update" "UPDATE_AVAILABLE" "use -AllowExternalUpdates or enable policy.apply_updates" }
            elseif ($Check.state -eq "REVIEW_REQUIRED") { Add-Step "External tools update" "REVIEW_REQUIRED" "manual policy decision is required" }
            else { Add-Step "External tools update" "CURRENT" "no allowed update is pending" }
        }
    }
    $Phase = "indexes"
    Emit-Result "WAITING_BACKGROUND" -CheckpointOnly
    $RlmOutput = (& (Join-Path $Root "scripts\start-rlm-tools-bsl.ps1") *>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "RLM MCP failed to start: $RlmOutput" }; Add-Step "RLM health" "OK" $(if ($RlmOutput) { $RlmOutput } else { "healthy" })
    $Indexes = Update-Indexes $LocalConfig ($ExtensionUpdate.state -eq "UPDATED")
    if ($Indexes.state -eq "RUNNING") {
        $NextActions = @([ordered]@{ action = "update"; parameters = @{ confirmed = $true; update_id = $UpdateId; wait_seconds = 30 } })
        Add-Step "RLM indexes" "RUNNING" "Background jobs are saved; resume the same update_id after waiting."
        Emit-Result "WAITING_BACKGROUND"; exit 0
    }
    Add-Step "RLM indexes" "CURRENT" "all configured source indexes are fresh"
    $Phase = "doctor"
    $DoctorOutput = (& $VenvPython (Join-Path $Root "scripts\flow1c.py") doctor --json --profile project-basic 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Add-Step "Doctor" "BLOCKED" $DoctorOutput; throw "Post-update doctor did not reach READY." }
    $DoctorResult = $DoctorOutput | ConvertFrom-Json
    if (-not $DoctorResult.ready) { throw "Post-update doctor returned an unexpected state: $DoctorOutput" }
    $SmokeTests = [ordered]@{ state = "PASSED"; doctor = "READY" }; Add-Step "Smoke tests" "PASSED" "RLM, indexes, skills and BSL checks passed"
    $FinalState = "READY"
    if ($ExternalState -in @("UPDATE_AVAILABLE", "REVIEW_REQUIRED")) { $FinalState = $ExternalState }
    $Phase = "complete"
    Emit-Result $FinalState; exit 0
} catch {
    if ($ResumeSnapshot -and -not $ResumeValidated) {
        $BackupDirectory = [string]$ResumeSnapshot.backup_directory
        $PreviousCommit = [string]$ResumeSnapshot.previous_commit; $CurrentCommit = [string]$ResumeSnapshot.current_commit
        $PreviousVersion = [string]$ResumeSnapshot.previous_version; $CurrentVersion = [string]$ResumeSnapshot.current_version
    }
    $FailureDetail = $_.Exception.Message
    if ($_.InvocationInfo.ScriptLineNumber) { $FailureDetail += " (scripts/update.ps1:$($_.InvocationInfo.ScriptLineNumber))" }
    $FailureState = if ($FailureDetail -like "*RECOVERY_REQUIRED:*") { "RECOVERY_REQUIRED" } else { "BLOCKED" }
    Add-Step "Update" $FailureState $FailureDetail; Emit-Result $FailureState $FailureDetail; exit 1
} finally {
    if ($UpdateLock) { $UpdateLock.Dispose() }
}
