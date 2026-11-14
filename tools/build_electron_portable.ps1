$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$electronVersion = "33.3.0"
$runtimeName = "electron-v$electronVersion-win32-x64"
$downloadUrl = "https://github.com/electron/electron/releases/download/v$electronVersion/$runtimeName.zip"
$cacheDir = Join-Path $root ".electron-cache"
$zipPath = Join-Path $cacheDir "$runtimeName.zip"
$runtimeDir = Join-Path $cacheDir $runtimeName
$distRoot = Join-Path $root "dist"
$appDist = Join-Path $distRoot "EZSlicer"
$nextDist = Join-Path $distRoot "EZSlicer.next"
$resourcesApp = Join-Path $nextDist "resources\app"

New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null

if (!(Test-Path $zipPath)) {
    Write-Host "Downloading Electron $electronVersion..."
    Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath
}

if (!(Test-Path $runtimeDir)) {
    Write-Host "Extracting Electron..."
    Expand-Archive -LiteralPath $zipPath -DestinationPath $runtimeDir
}

if (Test-Path $nextDist) {
    Remove-Item -LiteralPath $nextDist -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $nextDist | Out-Null

Write-Host "Building renderer..."
Push-Location $root
try {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) {
        throw "npm run build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "Copying Electron runtime..."
# Copy into an empty folder, one top-level item at a time: PowerShell 7's wildcard copy fails
# ("Container cannot be copied onto existing leaf item") when a target folder already exists.
Get-ChildItem -LiteralPath $runtimeDir -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $nextDist $_.Name) -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $resourcesApp | Out-Null

Write-Host "Copying app files..."
$items = @(
    "assets",
    "electron",
    "resin_slicer",
    "src",
    "package.json",
    "pyproject.toml",
    "README.md",
    "LICENSE"
)
foreach ($item in $items) {
    $src = Join-Path $root $item
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination $resourcesApp -Recurse -Force
    }
}

# Only the built renderer from dist\: the app folders being built live in dist\ too, so copying
# all of it would copy the build into itself.
$rendererDist = Join-Path $resourcesApp "dist"
New-Item -ItemType Directory -Force -Path $rendererDist | Out-Null
Copy-Item -LiteralPath (Join-Path $distRoot "index.html") -Destination $rendererDist -Force
Copy-Item -LiteralPath (Join-Path $distRoot "assets") -Destination (Join-Path $rendererDist "assets") -Recurse -Force

Get-ChildItem -Path $resourcesApp -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -Path $resourcesApp -Recurse -Filter "*.pyc" |
    Remove-Item -Force

$electronExe = Join-Path $nextDist "electron.exe"
$resinExe = Join-Path $nextDist "EZSlicer.exe"
if (Test-Path $resinExe) {
    Remove-Item -LiteralPath $resinExe -Force
}
Rename-Item -LiteralPath $electronExe -NewName "EZSlicer.exe"

# version.txt next to the exe (first line = the bare version) so jET Hangar can see what's installed.
$version = (Get-Content -Raw -LiteralPath (Join-Path $root "package.json") | ConvertFrom-Json).version
[System.IO.File]::WriteAllText((Join-Path $nextDist "version.txt"), "$version`n")
# BUILD.json lets the engine report its commit without git (GET /api/version).
$build = [ordered]@{
    app = "ezslicer"
    version = $version
    commit = (& git -C $root rev-parse HEAD).Trim()
    date = (& git -C $root log -1 --format=%cI).Trim()
    tag = "$(& git -C $root tag --points-at HEAD --list 'v[0-9]*')".Trim()
}
[System.IO.File]::WriteAllText((Join-Path $resourcesApp "resin_slicer/BUILD.json"), ($build | ConvertTo-Json) + "`n")

$oldDist = Join-Path $distRoot ("EZSlicer.old-" + (Get-Date -Format "yyyyMMddHHmmss"))
if (Test-Path $appDist) {
    try {
        Rename-Item -LiteralPath $appDist -NewName (Split-Path -Leaf $oldDist) -ErrorAction Stop
    } catch {
        Write-Error "Could not replace the existing app. Close every running EZSlicer window and rerun this script. The staged build is at: $nextDist"
    }
}
Rename-Item -LiteralPath $nextDist -NewName "EZSlicer"
if (Test-Path $oldDist) {
    try {
        Remove-Item -LiteralPath $oldDist -Recurse -Force -ErrorAction Stop
    } catch {
        Write-Warning "Built the new app, but could not remove the old folder: $oldDist"
    }
}

Write-Host "Built portable Electron app:"
Write-Host (Join-Path $appDist "EZSlicer.exe")
