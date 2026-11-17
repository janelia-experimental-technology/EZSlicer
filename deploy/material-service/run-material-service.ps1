# Supervisor for the EZSlicer material service on the host (runs as the scheduled task).
# Keeps one `python -m resin_slicer.material_service` running, restarting it if it exits.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Program Files\Python312\python.exe"
$logDir = Join-Path $root "logs"
$dataDir = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $logDir, $dataDir | Out-Null

# Stop-ScheduledTask ends only this script; clear a service left over from a previous run.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "resin_slicer\.material_service" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$tokenFile = Join-Path $root "token.txt"
if (Test-Path $tokenFile) { $env:MATERIAL_SERVICE_TOKEN = (Get-Content $tokenFile -Raw).Trim() }
$webhookFile = Join-Path $root "webhook.txt"
if (Test-Path $webhookFile) { $env:MATERIAL_SERVICE_WEBHOOK = (Get-Content $webhookFile -Raw).Trim() }
# jET's stats: the console agent on this host, signed in with its own token (no new key)
$consoleToken = "C:\ProgramData\AssetLoans\secrets\console_token.txt"
if (Test-Path $consoleToken) {
    $env:MATERIAL_SERVICE_STATS_URL = "http://127.0.0.1:8020/api/v1/stats/jobs"
    $env:MATERIAL_SERVICE_STATS_TOKEN_FILE = $consoleToken
}
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $root "app"

while ($true) {
    $log = Join-Path $logDir ("service-" + (Get-Date -Format "yyyyMMdd") + ".log")
    Add-Content -Path $log -Value ("[{0}] starting" -f (Get-Date -Format s))
    # Through cmd so the log stays plain UTF-8 (PowerShell 5.1 redirection writes UTF-16).
    cmd.exe /d /c "`"$python`" -m resin_slicer.material_service --host 0.0.0.0 --port 8070 --data `"$dataDir`" --poll 5 >> `"$log`" 2>&1"
    Add-Content -Path $log -Value ("[{0}] exited with {1}; restarting in 5 s" -f (Get-Date -Format s), $LASTEXITCODE)
    Start-Sleep -Seconds 5
}
