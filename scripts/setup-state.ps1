[CmdletBinding()]
param(
    [switch]$Json,
    [ValidateSet("conversation", "project-basic", "documents", "analysis", "implementation", "full")][string]$Profile = "analysis"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LocalConfigPath = Join-Path $Root ".flow1c.local.json"
$WorkflowConfigPath = Join-Path $Root ".flow1c.json"

function New-SetupError([string]$Code, [string]$Message, [string]$NextAction, [bool]$Recoverable = $true) {
    return [ordered]@{ code = $Code; message = $Message; recoverable = $Recoverable; next_action = $NextAction }
}

trap {
    $Failure = [ordered]@{
        schema_version = 1
        state = "BLOCKED"
        ready = $false
        requested_profile = $Profile
        errors = @(New-SetupError "SETUP_AUDIT_FAILED" $_.Exception.Message "Correct the reported environment problem and rerun setup-state.ps1 -Json -Profile $Profile.")
    }
    if ($Json) { $Failure | ConvertTo-Json -Depth 8 } else { [Console]::Error.WriteLine($_.Exception.Message) }
    exit 1
}

function Get-CommandPath([string]$Name) {
    $Command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    return ""
}

function Invoke-ExternalText([string]$FilePath, [string[]]$Arguments) {
    try {
        $Text = ((& $FilePath @Arguments 2>$null) | Out-String).Trim()
        return [ordered]@{ text = $Text; exit_code = $LASTEXITCODE; exception = "" }
    }
    catch {
        return [ordered]@{ text = ""; exit_code = 1; exception = $_.Exception.Message }
    }
}

function Get-JsonFile([string]$Path, [System.Collections.Generic.List[object]]$Errors) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        $Errors.Add((New-SetupError "CONFIG_JSON_INVALID" "Cannot parse $Path`: $($_.Exception.Message)" "Repair or replace the invalid JSON file, then resume setup."))
        return $null
    }
}

function Get-ContainingWorkflowRoot([string]$Path) {
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return "" }
    $Probe = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::HasExtension($Probe)) { $Probe = Split-Path -Parent $Probe }
    while ($Probe) {
        if ((Test-Path -LiteralPath (Join-Path $Probe ".flow1c.json") -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $Probe "scripts\flow1c.py") -PathType Leaf)) {
            return $Probe
        }
        $Parent = Split-Path -Parent $Probe
        if (-not $Parent -or $Parent -eq $Probe) { break }
        $Probe = $Parent
    }
    return ""
}

function Get-PathHint([object]$Config, [string]$Name) {
    $Value = ""
    if ($Config -and $Config.PSObject.Properties.Name -contains $Name) {
        $Value = ([string]$Config.$Name).Trim()
    }
    $Absolute = [bool]($Value -and [IO.Path]::IsPathRooted($Value))
    $ContainingWorkflow = if ($Absolute) { Get-ContainingWorkflowRoot $Value } else { "" }
    return [ordered]@{
        value = $Value
        exists = [bool]($Value -and (Test-Path -LiteralPath $Value))
        absolute = $Absolute
        containing_workflow = $ContainingWorkflow
        acceptable_location = [bool]($Absolute -and -not $ContainingWorkflow)
    }
}

$Errors = New-Object 'System.Collections.Generic.List[object]'
$LocalConfig = Get-JsonFile $LocalConfigPath $Errors
$WorkflowConfig = Get-JsonFile $WorkflowConfigPath $Errors

$GitPath = Get-CommandPath "git"
$WinGetPath = Get-CommandPath "winget"
$PythonLauncher = Get-CommandPath "py"
$PythonArgs = @()
if ($PythonLauncher) {
    $CompatibleLauncher = $false
    foreach ($Candidate in @("3", "3.14", "3.13", "3.12", "3.11", "3.10")) {
        $Probe = Invoke-ExternalText $PythonLauncher @("-$Candidate", "-c", "import sys")
        if ($Probe.exit_code -eq 0) { $PythonArgs = @("-$Candidate"); $CompatibleLauncher = $true; break }
    }
    if (-not $CompatibleLauncher) { $PythonLauncher = "" }
}
if (-not $PythonLauncher) {
    $PythonLauncher = Get-CommandPath "python"
    $PythonArgs = @()
}
$PythonVersion = ""
$PythonOk = $false
if ($PythonLauncher -and $PythonLauncher -notlike "*\WindowsApps\python.exe") {
    $VersionProbe = Invoke-ExternalText $PythonLauncher @($PythonArgs + @("-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"))
    $PythonVersion = $VersionProbe.text
    if ($VersionProbe.exit_code -eq 0 -and $PythonVersion) {
        $PythonOk = ([version]$PythonVersion -ge [version]"3.10")
    }
    elseif ($VersionProbe.exception) {
        $Errors.Add((New-SetupError "PYTHON_VERSION_UNAVAILABLE" "Cannot determine Python version: $($VersionProbe.exception)" "Install Python 3.10 or newer, or provide an approved offline installer."))
    }
}

$WorkflowUrl = ""
$GitUserName = ""
$GitUserEmail = ""
if ($GitPath) {
    $OriginProbe = Invoke-ExternalText $GitPath @("-C", $Root, "config", "--get", "remote.origin.url")
    $WorkflowUrl = $OriginProbe.text
    if ($OriginProbe.exit_code -ne 0) { $Errors.Add((New-SetupError "WORKFLOW_GIT_UNAVAILABLE" "Git could not read workflow origin (exit $($OriginProbe.exit_code)); ZIP or non-repository checkout is supported for bootstrap only." "Use conversation bootstrap from ZIP, or open a valid Git checkout before project setup/update.")) }
    $GitUserName = (Invoke-ExternalText $GitPath @("-C", $Root, "config", "user.name")).text
    $GitUserEmail = (Invoke-ExternalText $GitPath @("-C", $Root, "config", "user.email")).text
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$RlmServer = Join-Path $Root ".venv\Scripts\rlm-tools-bsl.exe"
$RlmIndex = Join-Path $Root ".venv\Scripts\rlm-bsl-index.exe"
$BslCommand = ""
if ($LocalConfig -and $LocalConfig.bsl_language_server) {
    $BslCommand = ([string]$LocalConfig.bsl_language_server.command).Trim()
}

$ExternalToolsSnapshot = [ordered]@{ state = "INVALID"; external_tools = @{}; error = "not checked" }
if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    try {
        $ExternalOutput = (& $VenvPython (Join-Path $Root "scripts\external_tools.py") --mode check --offline --json 2>&1 | Out-String).Trim()
        $ExternalToolsSnapshot = $ExternalOutput | ConvertFrom-Json
    }
    catch {
        $ExternalToolsSnapshot = [ordered]@{ state = "INVALID"; external_tools = @{}; error = $_.Exception.Message }
    }
}

$GiteaBaseUrl = ""
$GiteaOwner = ""
$GiteaRepository = ""
if ($WorkflowConfig -and $WorkflowConfig.gitea) {
    $GiteaBaseUrl = ([string]$WorkflowConfig.gitea.base_url).Trim()
    $GiteaOwner = ([string]$WorkflowConfig.gitea.owner).Trim()
    $GiteaRepository = ([string]$WorkflowConfig.gitea.repository).Trim()
}
if ($LocalConfig -and $LocalConfig.gitea) {
    if ($LocalConfig.gitea.base_url) { $GiteaBaseUrl = ([string]$LocalConfig.gitea.base_url).Trim() }
    if ($LocalConfig.gitea.owner) { $GiteaOwner = ([string]$LocalConfig.gitea.owner).Trim() }
    if ($LocalConfig.gitea.repository) { $GiteaRepository = ([string]$LocalConfig.gitea.repository).Trim() }
}

$MissingPrerequisites = @()
if ($Profile -ne "conversation" -and -not $GitPath) { $MissingPrerequisites += "git" }
if (-not $PythonOk) { $MissingPrerequisites += "python>=3.10" }
$BootstrapMissing = @()
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) { $BootstrapMissing += ".venv" }
if ($Profile -in @("analysis", "implementation", "full")) {
    if (-not (Test-Path -LiteralPath $RlmServer -PathType Leaf)) { $BootstrapMissing += "rlm-tools-bsl" }
    if (-not (Test-Path -LiteralPath $RlmIndex -PathType Leaf)) { $BootstrapMissing += "rlm-bsl-index" }
}
if ($Profile -in @("implementation", "full") -and -not ($BslCommand -and (Test-Path -LiteralPath $BslCommand -PathType Leaf))) { $BootstrapMissing += "bsl-language-server" }
if ($Profile -eq "full" -and $ExternalToolsSnapshot.cc_1c_skills.state -ne "OK") { $BootstrapMissing += "cc-1c-skills" }

$State = "NEEDS_INPUT_CONFIRMATION"
if ($MissingPrerequisites.Count -gt 0) {
    $State = if ($WinGetPath) { "NEEDS_PREREQUISITE_APPROVAL" } else { "NEEDS_OFFLINE_INSTALLERS" }
}
elseif ($BootstrapMissing.Count -gt 0) {
    $State = "NEEDS_BOOTSTRAP_APPROVAL"
}
elseif ($Profile -eq "conversation") {
    $State = "READY"
}

[object[]]$RequiredConfirmations = @()
if ($Profile -ne "conversation") {
    $RequiredConfirmations = @(
        "project_reference",
        "workflow_repository_url",
        "documentation_repository_url_or_initialize_empty_repository",
        "documentation_path",
        "configuration_path",
        "extension_mode",
        "extension_path",
        "extension_repository_url_when_git",
        "gitea_base_url",
        "gitea_owner",
        "gitea_repository",
        "git_identity_when_missing"
    )
}

[object[]]$NextActions = @()
if ($State -eq "NEEDS_PREREQUISITE_APPROVAL") { $NextActions = @("Review and explicitly approve the prerequisite installation plan.") }
elseif ($State -eq "NEEDS_OFFLINE_INSTALLERS") { $NextActions = @("Provide absolute paths to organization-approved offline Git/Python installers.") }
elseif ($State -eq "NEEDS_BOOTSTRAP_APPROVAL") { $NextActions = @("Run bootstrap.ps1 -Profile $Profile -Plan -Json, then explicitly approve that plan.") }
elseif ($State -eq "NEEDS_INPUT_CONFIRMATION") { $NextActions = @("Confirm the complete project path and URL set, then run configure-project.ps1.") }

$Result = [ordered]@{
    schema_version = 1
    state = $State
    ready = ($State -eq "READY")
    requested_profile = $Profile
    next_actions = [object[]]$NextActions
    confirmation_required = ($State -ne "READY")
    note = $(if ($State -eq "READY") { "The requested profile is ready." } else { "Existing local values are suggestions only and must be confirmed in this setup session." })
    prerequisites = [ordered]@{
        powershell = [ordered]@{ ok = ($PSVersionTable.PSVersion.Major -ge 5); version = [string]$PSVersionTable.PSVersion }
        git = [ordered]@{ ok = [bool]$GitPath; path = $GitPath }
        python = [ordered]@{ ok = $PythonOk; path = $PythonLauncher; version = $PythonVersion }
        winget = [ordered]@{ ok = [bool]$WinGetPath; path = $WinGetPath }
        missing = $MissingPrerequisites
    }
    bootstrap = [ordered]@{
        complete = ($BootstrapMissing.Count -eq 0)
        missing = $BootstrapMissing
        venv_python = $VenvPython
        rlm_server = $RlmServer
        rlm_index = $RlmIndex
        bsl_language_server = $BslCommand
    }
    external_tools = $ExternalToolsSnapshot
    suggestions = [ordered]@{
        project_reference = if ($LocalConfig) { [string]$LocalConfig.project_reference } else { "" }
        workflow_repository_url = $WorkflowUrl
        documentation_repository_url = if ($LocalConfig) { [string]$LocalConfig.documentation_repository_url } else { "" }
        documentation_path = Get-PathHint $LocalConfig "documentation_path"
        configuration_path = Get-PathHint $LocalConfig "configuration_path"
        extension_mode = if ($LocalConfig) { [string]$LocalConfig.extension_mode } else { "" }
        extension_repository_url = if ($LocalConfig) { [string]$LocalConfig.extension_repository_url } else { "" }
        extension_path = Get-PathHint $LocalConfig "extension_path"
        functional_spec_template = Get-PathHint $LocalConfig "functional_spec_template"
        gitea = [ordered]@{ base_url = $GiteaBaseUrl; owner = $GiteaOwner; repository = $GiteaRepository }
        git_identity = [ordered]@{ user_name = $GitUserName; user_email = $GitUserEmail }
    }
    required_confirmations = [object[]]$RequiredConfirmations
    optional_inputs = @(
        "functional_spec_template"
    )
    errors = $Errors
}

if ($Json) {
    $Result | ConvertTo-Json -Depth 8
}
else {
    Write-Host "FLOW1C setup state: $State"
    if ($State -ne "READY") { Write-Host "Confirmation of all external inputs is required for this run." }
    if ($MissingPrerequisites.Count) { Write-Host "Missing prerequisites: $($MissingPrerequisites -join ', ')" }
    if ($BootstrapMissing.Count) { Write-Host "Missing bootstrap components: $($BootstrapMissing -join ', ')" }
    if ($Errors.Count) { $Errors | ForEach-Object { Write-Host "ERROR [$($_.code)]: $($_.message)" } }
}
