# SSH-facing side of the bridge: hands one request to the worker through the
# scheduled task, waits for it, and prints the result.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File qbxml-request.ps1 -Request C:\qbd-bridge\io\req_x.xml -TimeoutSec 900
#
# Output lines:  STATUS: OK | STATUS: ERR: <message>   and, on OK,  RESPONSE: <path>
# The caller fetches the response, then deletes it (remote.sh does both).
#
# The bridge runs one request at a time. The lock file names its owner and
# an expiry, so a waiting caller never breaks a request that's still inside
# its own time limit, and only the owner removes it. Each request gets its own
# request, response and status files, so a late answer can't be read as the
# answer to the next request.
param(
    [Parameter(Mandatory)][string]$Request,
    [int]$TimeoutSec = 400,
    [string]$IoDir = "$PSScriptRoot\io",
    [string]$TaskName = "qbd-archive bridge"
)
$ErrorActionPreference = "Stop"
$lock = Join-Path $IoDir "bridge.lock"
$id = [guid]::NewGuid().ToString("N")
$expires = (Get-Date).AddSeconds($TimeoutSec + 180)

function Read-Lock {
    try { return (Get-Content $lock -Raw -ErrorAction Stop).Trim() -split '\s+' } catch { return $null }
}

# take the lock: wait while another live request holds it
$giveUp = (Get-Date).AddSeconds($TimeoutSec + 600)
while ($true) {
    try {
        $fs = [IO.File]::Open($lock, 'CreateNew', 'Write')
        $w = New-Object IO.StreamWriter($fs)
        $w.Write("$id $($expires.ToString('o'))")
        $w.Close()
        break
    } catch [IO.IOException] {
        $held = Read-Lock
        if ($held -and $held.Count -ge 2 -and [datetime]$held[1] -lt (Get-Date)) {
            Remove-Item $lock -Force -ErrorAction SilentlyContinue   # holder's own expiry passed
            continue
        }
        if ((Get-Date) -gt $giveUp) { "STATUS: ERR: bridge locked"; exit 1 }
        Start-Sleep -Seconds 5
    }
}
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $ready = (Get-Date).AddSeconds(30)
    while ((Get-ScheduledTask -TaskName $TaskName).State -ne "Ready" -and (Get-Date) -lt $ready) {
        Start-Sleep -Milliseconds 500
    }
    Move-Item $Request (Join-Path $IoDir "request_$id.xml") -Force
    Set-Content -Path (Join-Path $IoDir "job.txt") -Value "$id $TimeoutSec" -Encoding ASCII
    $status = Join-Path $IoDir "status_$id.txt"
    Start-ScheduledTask -TaskName $TaskName
    $end = (Get-Date).AddSeconds($TimeoutSec + 60)
    while (-not (Test-Path $status) -and (Get-Date) -lt $end) { Start-Sleep -Seconds 2 }
    Start-Sleep -Milliseconds 300   # let the worker finish writing
    $s = if (Test-Path $status) { (Get-Content $status -Raw) } else { $null }
    if (-not $s) { $s = "ERR: worker did not report (timeout)" }
    $s = $s.Trim()
    "STATUS: $s"
    if ($s -eq "OK") { "RESPONSE: " + (Join-Path $IoDir "response_$id.xml") }
    Remove-Item (Join-Path $IoDir "request_$id.xml"), $status -ErrorAction SilentlyContinue
    if ($s -ne "OK") { Remove-Item (Join-Path $IoDir "response_$id.xml") -ErrorAction SilentlyContinue }
} finally {
    $held = Read-Lock
    if ($held -and $held[0] -eq $id) { Remove-Item $lock -Force -ErrorAction SilentlyContinue }
}
