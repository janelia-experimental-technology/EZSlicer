# Runs on the host (as an administrator) after deploy.ps1 has copied the files into place.
# Creates the write token once, the firewall rule and the scheduled task, then (re)starts it.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "EZSlicer Material Service"

$tokenFile = Join-Path $root "token.txt"
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    Set-Content -Path $tokenFile -Value ("ms-" + [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")) -Encoding ascii -NoNewline
    icacls $tokenFile /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" | Out-Null
    Write-Output "Created a new write token in $tokenFile"
}

if (-not (Get-NetFirewallRule -DisplayName "$taskName 8070" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "$taskName 8070" -Direction Inbound -Protocol TCP -LocalPort 8070 -Action Allow -Profile Domain,Private | Out-Null
    Write-Output "Opened TCP 8070"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f (Join-Path $root "run-material-service.ps1")) -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT45S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $taskName
Write-Output "Started task '$taskName'"
