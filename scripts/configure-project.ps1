[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WorkflowRepositoryUrl,
    [Parameter(Mandatory = $true)][string]$DocumentationPath,
    [string]$DocumentationRepositoryUrl = "",
    [Parameter(Mandatory = $true)][ValidateSet("Git", "LocalExport")][string]$ExtensionMode,
    [Parameter(Mandatory = $true)][string]$ExtensionPath,
    [string]$ExtensionRepositoryUrl = "",
    [string]$ExtensionBranchPrefix = "",
    [Parameter(Mandatory = $true)][string]$ConfigurationPath,
    [string]$FunctionalSpecTemplate = "",
    [Parameter(Mandatory = $true)][string]$GiteaBaseUrl,
    [Parameter(Mandatory = $true)][string]$GiteaOwner,
    [Parameter(Mandatory = $true)][string]$GiteaRepository,
    [string]$GitUserName = "",
    [string]$GitUserEmail = "",
    [string]$ProjectReference = "",
    [ValidateSet("conversation", "project-basic", "documents", "analysis", "implementation", "full")][string]$Profile = "analysis",
    [string]$SetupId = "",
    [switch]$Confirmed,
    [switch]$Json,
    [switch]$InitializeDocumentationRepository,
    [switch]$AllowSharedWorkflowDocumentationRepository
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LocalConfigPath = Join-Path $Root ".flow1c.local.json"
$TemporaryConfigPath = ""
$PreviousConfigPath = ""
$ConfigReplaced = $false
function Write-ProgressMessage([string]$Message) { if (-not $Json) { Write-Host $Message } }

trap {
    if ($ConfigReplaced) {
        if ($PreviousConfigPath -and (Test-Path -LiteralPath $PreviousConfigPath -PathType Leaf)) {
            Move-Item -LiteralPath $PreviousConfigPath -Destination $LocalConfigPath -Force -ErrorAction SilentlyContinue
        }
        else {
            Remove-Item -LiteralPath $LocalConfigPath -Force -ErrorAction SilentlyContinue
        }
    }
    if ($TemporaryConfigPath) { Remove-Item -LiteralPath $TemporaryConfigPath -Force -ErrorAction SilentlyContinue }
    $Failure = [ordered]@{
        schema_version = 1
        state = "BLOCKED"
        ready = $false
        setup_id = $SetupId
        requested_profile = $Profile
        errors = @([ordered]@{
            code = "PROJECT_CONFIGURATION_FAILED"
            message = $_.Exception.Message
            recoverable = $true
            next_action = "Correct the reported input or environment issue and run setup-resume with the same setup_id."
        })
        next_action = "setup-resume"
    }
    if ($Json) { $Failure | ConvertTo-Json -Depth 8 } else { [Console]::Error.WriteLine($_.Exception.Message) }
    exit 1
}

function Resolve-FullPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw "External paths must be absolute: $Path"
    }
    return [IO.Path]::GetFullPath($Path)
}

function Get-RepositoryUrl([string]$Path) {
    if (-not (Test-Path -LiteralPath (Join-Path $Path ".git"))) {
        return ""
    }
    $Url = git -C $Path config --get remote.origin.url
    if ($LASTEXITCODE -ne 0) { return "" }
    return ([string]$Url).Trim()
}

. (Join-Path $PSScriptRoot "git-identity.ps1")

function Assert-ExternalPath([string]$Name, [string]$Path) {
    $FullPath = Resolve-FullPath $Path
    $RootPrefix = $Root.TrimEnd("\", "/") + [IO.Path]::DirectorySeparatorChar
    if ($FullPath.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or
        $FullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name must be outside the Flow1C checkout: $FullPath"
    }

    $Probe = if (Test-Path -LiteralPath $FullPath -PathType Leaf) { Split-Path -Parent $FullPath } else { $FullPath }
    while ($Probe) {
        $Marker = Join-Path $Probe ".flow1c.json"
        $Cli = Join-Path $Probe "scripts\flow1c.py"
        if ((Test-Path -LiteralPath $Marker -PathType Leaf) -and (Test-Path -LiteralPath $Cli -PathType Leaf)) {
            throw "$Name points into another Flow1C checkout: $Probe"
        }
        $Parent = Split-Path -Parent $Probe
        if (-not $Parent -or $Parent -eq $Probe) { break }
        $Probe = $Parent
    }
    return $FullPath
}

function Assert-RepositoryRemote([string]$Name, [string]$Path, [string]$ExpectedUrl) {
    $ActualUrl = Get-RepositoryUrl $Path
    if (-not $ActualUrl) {
        throw "$Name repository has no origin URL: $Path"
    }
    if ((Normalize-GitUrl $ActualUrl) -ne (Normalize-GitUrl $ExpectedUrl)) {
        throw "$Name repository origin '$ActualUrl' does not match the confirmed URL '$ExpectedUrl'."
    }
}

function Connect-Repository([string]$Name, [string]$Url, [string]$Path, [bool]$CanInitialize) {
    $FullPath = Resolve-FullPath $Path
    if (Test-Path -LiteralPath (Join-Path $FullPath ".git")) {
        if ($Url) { Assert-RepositoryRemote $Name $FullPath $Url }
        Write-ProgressMessage "$Name repository already connected: $FullPath"
        return $FullPath
    }
    if (Test-Path -LiteralPath $FullPath) {
        $Entries = @(Get-ChildItem -LiteralPath $FullPath -Force)
        if ($Entries.Count -gt 0) {
            throw "$Name target exists, is not a Git repository and is not empty: $FullPath"
        }
    }
    if ($Url) {
        $Parent = Split-Path -Parent $FullPath
        $null = New-Item -ItemType Directory -Force -Path $Parent
        git clone -- $Url $FullPath | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Cannot clone $Name repository from $Url." }
        return $FullPath
    }
    if ($CanInitialize) {
        $null = New-Item -ItemType Directory -Force -Path $FullPath
        git -C $FullPath init -b main | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Cannot initialize documentation repository at $FullPath." }
        return $FullPath
    }
    throw "$Name repository is missing. Provide its URL or an existing local clone."
}

function Test-Contains1cSources([string]$Path) {
    foreach ($File in [IO.Directory]::EnumerateFiles($Path, "*", [IO.SearchOption]::AllDirectories)) {
        if ([IO.Path]::GetExtension($File) -in @(".bsl", ".xml", ".mdo")) { return $true }
    }
    return $false
}

function Resolve-OneCSourceRoot([string]$Path) {
    $Resolved = (Resolve-Path -LiteralPath $Path).Path
    if (Test-Path -LiteralPath (Join-Path $Resolved "Configuration.xml") -PathType Leaf) {
        return $Resolved
    }
    $Candidates = @(
        [IO.Directory]::EnumerateFiles($Resolved, "Configuration.xml", [IO.SearchOption]::AllDirectories) |
            Where-Object { $_ -notmatch '[\\/](\.git|\.tools|\.venv|\.workspace|build|node_modules)[\\/]' }
    )
    if ($Candidates.Count -eq 1) {
        return (Split-Path -Parent $Candidates[0])
    }
    if ($Candidates.Count -gt 1) {
        throw "Multiple 1C source roots were found under '$Resolved'. Configure a checkout containing one Configuration.xml source tree."
    }
    return $Resolved
}

function Ensure-RlmIndex([string]$SourcePath) {
    Write-ProgressMessage "Checking RLM index: $SourcePath"
    $Output = (& (Join-Path $PSScriptRoot "rlm-index.ps1") -Action Ensure -SourcePath $SourcePath -Json | Out-String).Trim()
    try { return $Output | ConvertFrom-Json }
    catch { throw "rlm-index.ps1 returned invalid JSON for $SourcePath`: $Output" }
}

function Set-JsonProperty([object]$Object, [string]$Name, [object]$Value) {
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required. Run scripts\install-prerequisites.ps1 first."
}
if (-not $DocumentationRepositoryUrl -and -not $InitializeDocumentationRepository) {
    throw "Confirm the documentation repository URL, or explicitly use -InitializeDocumentationRepository."
}
if ($DocumentationRepositoryUrl -and $InitializeDocumentationRepository) {
    throw "Use either -DocumentationRepositoryUrl or -InitializeDocumentationRepository, not both."
}
if ($ExtensionMode -eq "Git" -and -not $ExtensionRepositoryUrl) {
    throw "ExtensionRepositoryUrl is required when ExtensionMode is Git."
}
if ($ExtensionMode -eq "LocalExport" -and $ExtensionRepositoryUrl) {
    throw "ExtensionRepositoryUrl must be empty when ExtensionMode is LocalExport."
}
if ($ExtensionBranchPrefix -and ($ExtensionBranchPrefix -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or
    $ExtensionBranchPrefix.Contains('..') -or $ExtensionBranchPrefix.EndsWith('/'))) {
    throw "ExtensionBranchPrefix must be a Git branch path prefix such as iss or feature/task."
}

$ActualWorkflowUrl = Get-RepositoryUrl $Root
if (-not $ActualWorkflowUrl) {
    throw "The Flow1C checkout has no origin URL; connect it to the confirmed workflow repository first."
}
if ((Normalize-GitUrl $ActualWorkflowUrl) -ne (Normalize-GitUrl $WorkflowRepositoryUrl)) {
    throw "The current Flow1C origin '$ActualWorkflowUrl' does not match the confirmed URL '$WorkflowRepositoryUrl'."
}
if ($DocumentationRepositoryUrl -and
    (Normalize-GitUrl $WorkflowRepositoryUrl) -eq (Normalize-GitUrl $DocumentationRepositoryUrl) -and
    -not $AllowSharedWorkflowDocumentationRepository) {
    throw "Flow1C and project documentation repositories must differ. Use -AllowSharedWorkflowDocumentationRepository only after explicit user confirmation."
}

$DocumentationPath = Assert-ExternalPath "Documentation path" $DocumentationPath
$ExtensionPath = Assert-ExternalPath "Extension path" $ExtensionPath
$ConfigurationPath = Assert-ExternalPath "Configuration path" $ConfigurationPath
if ($FunctionalSpecTemplate) {
    $FunctionalSpecTemplate = Assert-ExternalPath "Functional specification template" $FunctionalSpecTemplate
}
if ($ProjectReference) {
    $ProjectReference = $ProjectReference.Trim()
    if ($ProjectReference.Length -gt 128 -or $ProjectReference -match '[\x00-\x1F\x7F]') {
        throw "ProjectReference must contain 1-128 visible characters and no control characters."
    }
}
if ($InitializeDocumentationRepository -and -not $Confirmed) {
    throw "InitializeDocumentationRepository requires explicit -Confirmed."
}
if (-not (Test-Path -LiteralPath $ConfigurationPath -PathType Container)) {
    throw "1C configuration directory not found: $ConfigurationPath"
}
if (-not (Test-Contains1cSources $ConfigurationPath)) {
    throw "1C configuration directory contains no XML/BSL sources: $ConfigurationPath"
}
if ($FunctionalSpecTemplate -and -not (Test-Path -LiteralPath $FunctionalSpecTemplate -PathType Leaf)) {
    throw "Functional specification template not found: $FunctionalSpecTemplate"
}
if ($FunctionalSpecTemplate -and [IO.Path]::GetExtension($FunctionalSpecTemplate) -ne ".docx") {
    throw "Functional specification template must be a DOCX file: $FunctionalSpecTemplate"
}

$DocumentationPath = Connect-Repository "Documentation" $DocumentationRepositoryUrl $DocumentationPath $InitializeDocumentationRepository.IsPresent
git -C $DocumentationPath rev-parse --verify --quiet HEAD | Out-Null
$DocumentationHasHead = $LASTEXITCODE -eq 0
if ($ExtensionMode -eq "Git") {
    $ExtensionPath = Connect-Repository "Extension" $ExtensionRepositoryUrl $ExtensionPath $false
}
else {
    if (-not (Test-Path -LiteralPath $ExtensionPath -PathType Container)) {
        throw "Local extension export directory not found: $ExtensionPath"
    }
    if (-not (Test-Contains1cSources $ExtensionPath)) {
        throw "Local extension export contains no XML/BSL sources: $ExtensionPath"
    }
}
$ConfigurationPath = (Resolve-Path -LiteralPath $ConfigurationPath).Path
$ExtensionPath = (Resolve-Path -LiteralPath $ExtensionPath).Path
$ConfigurationSourcePath = Resolve-OneCSourceRoot $ConfigurationPath
$ExtensionSourcePath = Resolve-OneCSourceRoot $ExtensionPath
if ($FunctionalSpecTemplate) {
    $FunctionalSpecTemplate = (Resolve-Path -LiteralPath $FunctionalSpecTemplate).Path
}

foreach ($RepositoryPath in @($DocumentationPath)) {
    if ($GitUserName) { git -C $RepositoryPath config user.name $GitUserName }
    if ($GitUserEmail) { git -C $RepositoryPath config user.email $GitUserEmail }
}
if ($ExtensionMode -eq "Git") {
    if ($GitUserName) { git -C $ExtensionPath config user.name $GitUserName }
    if ($GitUserEmail) { git -C $ExtensionPath config user.email $GitUserEmail }
}

$RlmServerCommand = Join-Path $Root ".venv\Scripts\rlm-tools-bsl.exe"
$RlmIndexCommand = Join-Path $Root ".venv\Scripts\rlm-bsl-index.exe"
if ($Profile -in @("analysis", "implementation", "full")) {
    if (-not (Test-Path -LiteralPath $RlmServerCommand -PathType Leaf) -or -not (Test-Path -LiteralPath $RlmIndexCommand -PathType Leaf)) {
        throw "The $Profile profile requires rlm-tools-bsl. Run scripts\bootstrap.ps1 -Profile $Profile first."
    }
    & (Join-Path $PSScriptRoot "start-rlm-tools-bsl.ps1") | Out-Null
    $Jobs = @()
    foreach ($Source in @($ConfigurationSourcePath, $ExtensionSourcePath) | Select-Object -Unique) {
        $IndexState = Ensure-RlmIndex $Source
        if ($IndexState.state -eq "RUNNING") {
            $Jobs += [ordered]@{ kind = "rlm-index"; source = $Source; state = "RUNNING"; pid = $IndexState.pid; stdout = $IndexState.stdout_log; stderr = $IndexState.stderr_log }
        }
        elseif ($IndexState.state -ne "FRESH") { throw "RLM index failed for $Source`: $($IndexState.detail)" }
    }
    if ($Jobs.Count -gt 0) {
        if ($SetupId) {
            $CheckpointPath = Join-Path $Root ".workspace\setup\$SetupId.json"
            if (Test-Path -LiteralPath $CheckpointPath -PathType Leaf) {
                $Checkpoint = Get-Content -LiteralPath $CheckpointPath -Raw -Encoding utf8 | ConvertFrom-Json
                $Checkpoint.state = "WAITING_BACKGROUND"
                $Checkpoint.pending_jobs = $Jobs
                $Checkpoint.updated_at = [DateTimeOffset]::Now.ToString("o")
                $Checkpoint | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath "$CheckpointPath.tmp" -Encoding utf8
                Move-Item -LiteralPath "$CheckpointPath.tmp" -Destination $CheckpointPath -Force
            }
        }
        [ordered]@{ schema_version = 1; state = "WAITING_BACKGROUND"; ready = $false; setup_id = $SetupId; requested_profile = $Profile; jobs = $Jobs; next_action = "setup-status" } | ConvertTo-Json -Depth 8
        exit 0
    }
}

if (Test-Path -LiteralPath $LocalConfigPath -PathType Leaf) {
    $LocalConfig = Get-Content -LiteralPath $LocalConfigPath -Raw -Encoding utf8 | ConvertFrom-Json
}
else {
    $LocalConfig = [pscustomobject]@{}
}
Set-JsonProperty $LocalConfig "schema_version" 2
if (-not ($LocalConfig.PSObject.Properties.Name -contains "template_library")) {
    Set-JsonProperty $LocalConfig "template_library" ([pscustomobject][ordered]@{ schema_version = 1; storage_kind = "documentation"; root = "document-templates"; library_id = $null })
}
if ($ProjectReference) { Set-JsonProperty $LocalConfig "project_reference" $ProjectReference.Trim() }
# A saved path may point to a newly recreated repository. Explicit initialization
# without HEAD must let the scaffold choose v2 if the directory contains only .git.
# The scaffold itself preserves any existing files and layout marker.
$PreserveLegacyDocumentationLayout = $LocalConfig.documentation_path -and ([IO.Path]::GetFullPath($LocalConfig.documentation_path) -eq [IO.Path]::GetFullPath($DocumentationPath)) -and (-not $InitializeDocumentationRepository -or $DocumentationHasHead)
Set-JsonProperty $LocalConfig "documentation_path" $DocumentationPath
Set-JsonProperty $LocalConfig "documentation_repository_url" $DocumentationRepositoryUrl
Set-JsonProperty $LocalConfig "extension_path" $ExtensionPath
Set-JsonProperty $LocalConfig "extension_repository_url" $ExtensionRepositoryUrl
Set-JsonProperty $LocalConfig "extension_mode" $(if ($ExtensionMode -eq "Git") { "git" } else { "local-export" })
if ($PSBoundParameters.ContainsKey("ExtensionBranchPrefix")) {
    Set-JsonProperty $LocalConfig "extension_branch_prefix" $ExtensionBranchPrefix
}
Set-JsonProperty $LocalConfig "configuration_path" $ConfigurationPath
Set-JsonProperty $LocalConfig "functional_spec_template" $FunctionalSpecTemplate

$Setup = [pscustomobject]@{}
Set-JsonProperty $Setup "validation_version" 1
Set-JsonProperty $Setup "confirmed_at" ([DateTimeOffset]::Now.ToString("o"))
Set-JsonProperty $Setup "workflow_root" $Root
Set-JsonProperty $Setup "workflow_repository_url" $WorkflowRepositoryUrl
Set-JsonProperty $Setup "documentation_repository_initialized_locally" $InitializeDocumentationRepository.IsPresent
Set-JsonProperty $Setup "allow_shared_workflow_documentation_repository" $AllowSharedWorkflowDocumentationRepository.IsPresent
Set-JsonProperty $LocalConfig "setup" $Setup

$Rlm = [pscustomobject]@{}
Set-JsonProperty $Rlm "endpoint" "http://127.0.0.1:9000/mcp"
Set-JsonProperty $Rlm "server_command" $RlmServerCommand
Set-JsonProperty $Rlm "index_command" $RlmIndexCommand
Set-JsonProperty $Rlm "indexed_paths" @($ConfigurationSourcePath, $ExtensionSourcePath)
Set-JsonProperty $LocalConfig "rlm" $Rlm

$ExistingGitea = $LocalConfig.gitea
if (-not $ExistingGitea) { $ExistingGitea = [pscustomobject]@{} }
Set-JsonProperty $ExistingGitea "base_url" $GiteaBaseUrl
Set-JsonProperty $ExistingGitea "owner" $GiteaOwner
Set-JsonProperty $ExistingGitea "repository" $GiteaRepository
Set-JsonProperty $ExistingGitea "token_env" "FLOW1C_GITEA_TOKEN"
Set-JsonProperty $LocalConfig "gitea" $ExistingGitea

. (Join-Path $PSScriptRoot "project-documentation.ps1")
$CreatedSeedFiles = @(Initialize-Flow1CDocumentation -DocumentationPath $DocumentationPath -PreserveLegacyLayout:$PreserveLegacyDocumentationLayout)
$LayoutPath = Join-Path $DocumentationPath ".flow1c\layout.json"
if (Test-Path -LiteralPath $LayoutPath -PathType Leaf) {
    $DocumentationLayout = Get-Content -LiteralPath $LayoutPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($DocumentationLayout.layout_version -eq 2) {
        Set-JsonProperty $LocalConfig.template_library "root" ".flow1c/document-templates"
    }
}


if (-not $DocumentationHasHead) {
    $ConfiguredName = git -C $DocumentationPath config user.name
    $ConfiguredEmail = git -C $DocumentationPath config user.email
    if (-not $ConfiguredName -or -not $ConfiguredEmail) {
        throw "The empty documentation repository needs Git user.name and user.email before its initial commit."
    }
    if ($CreatedSeedFiles.Count -eq 0) {
        throw "The documentation repository has no initial commit and no scaffold files were created."
    }
    git -C $DocumentationPath add -- @CreatedSeedFiles
    if ($LASTEXITCODE -ne 0) { throw "Cannot stage documentation scaffold files." }
    git -C $DocumentationPath commit -m "chore: initialize FLOW1C project documentation" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot create the initial documentation commit." }
}

$TemporaryConfigPath = "$LocalConfigPath.$([Guid]::NewGuid().ToString('N')).tmp"
$PreviousConfigPath = "$LocalConfigPath.$([Guid]::NewGuid().ToString('N')).previous"
if (Test-Path -LiteralPath $LocalConfigPath -PathType Leaf) { Copy-Item -LiteralPath $LocalConfigPath -Destination $PreviousConfigPath }
$LocalConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $TemporaryConfigPath -Encoding utf8
Move-Item -LiteralPath $TemporaryConfigPath -Destination $LocalConfigPath -Force
$ConfigReplaced = $true

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython (Join-Path $PSScriptRoot "flow1c.py") doctor --profile $Profile --json | Out-Null
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $PreviousConfigPath -PathType Leaf) { Move-Item -LiteralPath $PreviousConfigPath -Destination $LocalConfigPath -Force }
    else { Remove-Item -LiteralPath $LocalConfigPath -Force -ErrorAction SilentlyContinue }
    $ConfigReplaced = $false
    throw "Project configuration was saved, but doctor found blocking errors."
}
if (Test-Path -LiteralPath $PreviousConfigPath -PathType Leaf) { Remove-Item -LiteralPath $PreviousConfigPath -Force }
$ConfigReplaced = $false

if ($SetupId) {
    $CheckpointPath = Join-Path $Root ".workspace\setup\$SetupId.json"
    if (Test-Path -LiteralPath $CheckpointPath -PathType Leaf) {
        $Checkpoint = Get-Content -LiteralPath $CheckpointPath -Raw -Encoding utf8 | ConvertFrom-Json
        $Checkpoint.state = "COMPLETE"
        $Checkpoint.completed_steps = @($Checkpoint.completed_steps) + "configure-project"
        $Checkpoint.pending_jobs = @()
        $Checkpoint.updated_at = [DateTimeOffset]::Now.ToString("o")
        $Checkpoint | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath "$CheckpointPath.tmp" -Encoding utf8
        Move-Item -LiteralPath "$CheckpointPath.tmp" -Destination $CheckpointPath -Force
    }
}
if ($Json) {
    [ordered]@{ schema_version = 1; state = "COMPLETE"; ready = $true; setup_id = $SetupId; requested_profile = $Profile; local_config = $LocalConfigPath } | ConvertTo-Json -Depth 6
} else {
    Write-ProgressMessage "Documentation repository: $DocumentationPath"
    Write-ProgressMessage "Extension source ($ExtensionMode): $ExtensionPath"
    Write-ProgressMessage "Configuration: $ConfigurationPath"
    Write-ProgressMessage "Local configuration: $LocalConfigPath"
}
