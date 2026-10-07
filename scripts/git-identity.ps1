# Shared repository identity policy. Loading this file performs no I/O.
function Normalize-GitUrl([string]$Url) {
    $Value = ([string]$Url).Trim().Replace("\", "/")
    if (-not $Value) { return "" }
    if ($Value -notmatch "://" -and $Value -match "^[^/@]+@[^:]+:.+$") {
        $Parts = $Value.Split(":", 2)
        $Value = "ssh://$($Parts[0])/$($Parts[1])"
    }
    try {
        $Parsed = [Uri]$Value
        $Identity = "$($Parsed.Host)/$($Parsed.AbsolutePath.Trim('/'))"
    }
    catch {
        $Identity = $Value.Trim("/")
    }
    return ($Identity -replace "\.git$", "").ToLowerInvariant()
}
