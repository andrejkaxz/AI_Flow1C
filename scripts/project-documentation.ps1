# Documentation scaffold data comes from templates/project-documentation.
# Loading this file only defines functions; existing documents are never replaced.

function Assert-DocumentationScaffoldPath([string]$Path) {
    $Probe = [IO.Path]::GetFullPath($Path)
    $Original = $Probe
    while ($Probe) {
        if (Test-Path -LiteralPath $Probe) {
            $Item = Get-Item -LiteralPath $Probe -Force -ErrorAction Stop
            if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Documentation scaffold path contains a symlink or junction: $Probe"
            }
            if ($Probe -ne $Original -and -not $Item.PSIsContainer) {
                throw "Documentation scaffold parent path is occupied by a file: $Probe"
            }
        }
        $Parent = Split-Path -Parent $Probe
        if (-not $Parent -or $Parent -eq $Probe) { break }
        $Probe = $Parent
    }
}

function Initialize-Flow1CDocumentation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DocumentationPath,
        [string]$TemplateRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) "templates\project-documentation")
    )

    if (-not [IO.Path]::IsPathRooted($DocumentationPath) -or
        -not [IO.Path]::IsPathRooted($TemplateRoot)) {
        throw "Documentation and scaffold paths must be absolute."
    }
    $Destination = [IO.Path]::GetFullPath($DocumentationPath)
    $Source = [IO.Path]::GetFullPath($TemplateRoot)
    if ($Destination -eq [IO.Path]::GetPathRoot($Destination) -or
        $Source -eq [IO.Path]::GetPathRoot($Source)) {
        throw "Documentation and scaffold paths must not be filesystem roots."
    }
    $Destination = $Destination.TrimEnd("\", "/")
    $Source = $Source.TrimEnd("\", "/")
    $Separator = [IO.Path]::DirectorySeparatorChar
    $SourcePrefix = $Source + $Separator
    $DestinationPrefix = $Destination + $Separator
    if ($Destination.Equals($Source, [StringComparison]::OrdinalIgnoreCase) -or
        $Destination.StartsWith($SourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $Source.StartsWith($DestinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Documentation and scaffold directories must not overlap."
    }
    Assert-DocumentationScaffoldPath $Source
    Assert-DocumentationScaffoldPath $Destination
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Documentation scaffold is missing: $Source"
    }

    # Validate every source and target before creating any scaffold file.
    $Pending = [Collections.Generic.Queue[string]]::new()
    $Pending.Enqueue($Source)
    $Plan = @()
    while ($Pending.Count) {
        foreach ($Item in Get-ChildItem -LiteralPath ($Pending.Dequeue()) -Force -ErrorAction Stop) {
            if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Documentation scaffold contains a symlink or junction: $($Item.FullName)"
            }
            if ($Item.PSIsContainer) { $Pending.Enqueue($Item.FullName); continue }
            if ($Item.Extension -ne ".md") { continue }
            $Relative = $Item.FullName.Substring($SourcePrefix.Length)
            $Target = [IO.Path]::GetFullPath((Join-Path $Destination $Relative))
            if (-not $Target.StartsWith($DestinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Documentation scaffold target escapes its directory."
            }
            Assert-DocumentationScaffoldPath $Target
            if (Test-Path -LiteralPath $Target -PathType Container) {
                throw "Documentation scaffold file path is occupied by a directory: $Target"
            }
            $Plan += [pscustomobject]@{ source = $Item.FullName; target = $Target; relative = $Relative }
        }
    }
    if (-not $Plan.Count) { throw "Documentation scaffold contains no Markdown files." }
    foreach ($Entry in $Plan | Sort-Object relative) {
        if (Test-Path -LiteralPath $Entry.target -PathType Leaf) { continue }
        Assert-DocumentationScaffoldPath $Entry.target
        $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Entry.target)
        [IO.File]::Copy($Entry.source, $Entry.target, $false)
        $Entry.relative
    }
}
