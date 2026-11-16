# Deploy (or update) the EZSlicer material service on a Windows host over ssh.
# Run from Windows PowerShell in the repo:  powershell -File deploy\material-service\deploy.ps1 [-HostName jetliquid-ww1]
# The host needs Python 3.12 at C:\Program Files\Python312 and key-based ssh as an administrator.
param(
    [string]$HostName = "jetliquid-ww1",
    [string]$Target = "C:/ProgramData/EZSlicerMaterials"
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("ezslicer-materials-" + [guid]::NewGuid().ToString("N"))
$pkg = Join-Path $stage "app\resin_slicer"
New-Item -ItemType Directory -Force -Path $pkg | Out-Null

# Only what the service imports (standard library only), with an __init__ that pulls in nothing else.
foreach ($name in "errors.py", "material_tags.py", "network_printers.py", "material_service.py") {
    Copy-Item (Join-Path $repo "resin_slicer\$name") $pkg
}
$version = (Select-String -Path (Join-Path $repo "resin_slicer\__init__.py") -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Set-Content -Path (Join-Path $pkg "__init__.py") -Value "`"`"`"EZSlicer material service (deployed subset of resin_slicer).`"`"`"`n__version__ = `"$version`"`n" -Encoding utf8
Copy-Item (Join-Path $PSScriptRoot "run-material-service.ps1"), (Join-Path $PSScriptRoot "install.ps1") $stage

function Invoke-Remote([string]$script) {
    # The remote shell is cmd, which would split on | and quotes: send PowerShell encoded.
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($script))
    ssh $HostName "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
}

$remoteStage = "$Target/staging"
Invoke-Remote "New-Item -ItemType Directory -Force -Path '$Target' | Out-Null; if (Test-Path '$remoteStage') { Remove-Item -Recurse -Force '$remoteStage' }; New-Item -ItemType Directory -Force -Path '$remoteStage' | Out-Null"
if ($LASTEXITCODE) { throw "Could not prepare $remoteStage on $HostName" }
# Copy the staged folder's contents into the existing staging dir (scp -r to a missing dir renames it).
scp -q -r "$stage\app" "$stage\run-material-service.ps1" "$stage\install.ps1" "${HostName}:$remoteStage/"
if ($LASTEXITCODE) { throw "Copy to $HostName failed" }
$install = @"
`$t = '$Target'; `$s = '$remoteStage'
if (-not (Test-Path "`$s/app/resin_slicer/material_service.py")) { throw 'staged files missing' }
if (Test-Path "`$t/app") { Remove-Item -Recurse -Force "`$t/app" }
Move-Item "`$s/app" "`$t/app"
Copy-Item "`$s/*.ps1" `$t -Force
Remove-Item -Recurse -Force `$s
& "`$t/install.ps1"
"@
Invoke-Remote $install
if ($LASTEXITCODE) { throw "Install on $HostName failed" }
Remove-Item -Recurse -Force $stage
Write-Output "Deployed resin_slicer $version material service to ${HostName}: http://${HostName}:8070/api/health"
