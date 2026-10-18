# One-time setup on the QuickBooks machine. Run in PowerShell as the Windows
# user who stays logged in to the desktop (the session QuickBooks runs in).
#
#   .\register-task.ps1 -CompanyFile C:\qb-archive\company.qbw
#
# Creates io\, records the company file path, restricts the bridge folder to
# this user, SYSTEM and Administrators (the folder will hold company data, and
# its scripts run as this user), and registers a scheduled task that runs the
# worker in this user's interactive session, only while the user is logged
# on. The task has no trigger; qbxml-request.ps1 starts it on demand.
param(
    [Parameter(Mandatory)][string]$CompanyFile,
    [string]$TaskName = "qbd-archive bridge"
)
$ErrorActionPreference = "Stop"
$io = Join-Path $PSScriptRoot "io"
New-Item -ItemType Directory -Force $io | Out-Null
Set-Content -Path (Join-Path $io "companyfile.txt") -Value $CompanyFile -Encoding ASCII

$me = "$env:USERDOMAIN\$env:USERNAME"
& icacls.exe $PSScriptRoot /inheritance:r /grant:r "${me}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not set permissions on $PSScriptRoot" }

$worker = Join-Path $PSScriptRoot "qbxml-worker.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$worker`""
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive
# The worker enforces each request's own timeout; this limit is only a backstop.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Settings $settings -Force | Out-Null
"registered '$TaskName' for $me; io at $io; folder restricted to $me, SYSTEM, Administrators"
