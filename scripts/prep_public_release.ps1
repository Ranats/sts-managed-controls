param(
    [string]$Version = "0.1.0-preview",
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$outputRoot = Join-Path $repoRoot $OutputDir
$stagingDir = Join-Path $outputRoot ("sts-managed-controls-" + $Version)
$zipPath = Join-Path $outputRoot ("sts-managed-controls-" + $Version + ".zip")

if (Test-Path $stagingDir) {
    Remove-Item -LiteralPath $stagingDir -Recurse -Force
}
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

New-Item -ItemType Directory -Path $stagingDir | Out-Null

function Copy-PublicPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $sourcePath = Join-Path $repoRoot $RelativePath
    $destinationPath = Join-Path $stagingDir $RelativePath

    if (-not (Test-Path $sourcePath)) {
        throw "Missing public release path: $RelativePath"
    }

    $destinationParent = Split-Path -Parent $destinationPath
    if ($destinationParent -and -not (Test-Path $destinationParent)) {
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    }

    Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Recurse -Force
}

$publicPaths = @(
    "README.md",
    "pyproject.toml",
    "src",
    "tests",
    "profiles\windows.example.json",
    "profiles\templates",
    "docs\release",
    "docs\workstreams\managed_runtime_controls.md",
    "tmp\codex_bridge_mod",
    "tmp\type_probe",
    "tmp\clrmd_probe"
)

foreach ($path in $publicPaths) {
    Copy-PublicPath -RelativePath $path
}

Get-ChildItem -LiteralPath (Join-Path $stagingDir "src") -Directory -Filter "*.egg-info" -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

$manifest = [ordered]@{
    version = $Version
    created_at = (Get-Date).ToString("s")
    repo_root = $repoRoot
    staging_dir = $stagingDir
    zip_path = $zipPath
    included_paths = $publicPaths
}

$manifestPath = Join-Path $stagingDir "release_manifest.json"
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

if (-not (Test-Path $outputRoot)) {
    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
}

Compress-Archive -LiteralPath $stagingDir -DestinationPath $zipPath -Force

Write-Output "release_staging=$stagingDir"
Write-Output "release_zip=$zipPath"
Write-Output "release_manifest=$manifestPath"
