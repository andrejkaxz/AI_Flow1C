# Loading this helper only defines functions. Scaffold/layout belongs to Python.
function Initialize-Flow1CDocumentation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DocumentationPath,
        [string]$TemplateRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) "templates\project-documentation"),
        [switch]$PreserveLegacyLayout
    )
    $ProductRoot = Split-Path -Parent $PSScriptRoot
    $Python = Join-Path $ProductRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { $Python = "python" }
    $Arguments = @((Join-Path $PSScriptRoot "initialize-documentation.py"), "--documentation", $DocumentationPath, "--templates", $TemplateRoot)
    if ($PreserveLegacyLayout) { $Arguments += "--preserve-legacy" }
    $Output = & $Python -X utf8 @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Documentation scaffold initialization failed." }
    $Paths = $Output | ConvertFrom-Json
    foreach ($Relative in $Paths) { $Relative.Replace("/", [IO.Path]::DirectorySeparatorChar) }
}
