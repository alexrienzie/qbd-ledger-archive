# Runs the current job inside the logged-on desktop session (launched by the
# "qbd-archive bridge" scheduled task, never by hand over SSH: the SDK can't
# start QuickBooks from a non-interactive session).
#
# Reads  <io>\job.txt  ("<id> <timeout seconds>"), <io>\request_<id>.xml,
#        <io>\companyfile.txt
# Writes <io>\response_<id>.xml and <io>\status_<id>.txt ("OK" or "ERR: ...")
#
# The SDK call runs in a separate process with a hard timeout; on timeout
# that process (not QuickBooks) is killed and the status says so.
param([string]$IoDir = "$PSScriptRoot\io")
$ErrorActionPreference = "Stop"
$log = Join-Path $IoDir "worker.log"
$id, $timeout = (Get-Content (Join-Path $IoDir "job.txt") -Raw).Trim() -split '\s+'
$timeout = [int]$timeout
$req = Join-Path $IoDir "request_$id.xml"
$resp = Join-Path $IoDir "response_$id.xml"
$status = Join-Path $IoDir "status_$id.txt"
$companyFile = (Get-Content (Join-Path $IoDir "companyfile.txt") -Raw).Trim()
Remove-Item $resp, $status -ErrorAction SilentlyContinue

$com = Join-Path $PSScriptRoot "qbxml-com.ps1"
$p = Start-Process -FilePath "powershell.exe" -PassThru -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$com`"",
    "-Request", "`"$req`"", "-Response", "`"$resp`"", "-Status", "`"$status`"",
    "-CompanyFile", "`"$companyFile`"")
if (-not $p.WaitForExit($timeout * 1000)) {
    & taskkill.exe /F /T /PID $p.Id 2>&1 | Out-Null
    Set-Content -Path $status -Value "ERR: timeout after $timeout s" -Encoding ASCII
}
if (-not (Test-Path $status)) {
    Set-Content -Path $status -Value "ERR: worker produced no status" -Encoding ASCII
}
$result = (Get-Content $status -Raw).Trim()
$bytes = if (Test-Path $resp) { (Get-Item $resp).Length } else { 0 }
Add-Content -Path $log -Value ("{0} {1} {2} bytes={3}" -f (Get-Date -Format s), $id, $result, $bytes)
