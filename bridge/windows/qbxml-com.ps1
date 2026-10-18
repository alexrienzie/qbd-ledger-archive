# One qbXML request through the QuickBooks SDK. Started by qbxml-worker.ps1
# as its own process, so the worker can kill it on timeout without touching
# QuickBooks itself.
param(
    [Parameter(Mandatory)][string]$Request,
    [Parameter(Mandatory)][string]$Response,
    [Parameter(Mandatory)][string]$Status,
    [Parameter(Mandatory)][string]$CompanyFile,
    [string]$AppName = "qbd-ledger-archive"
)
$ErrorActionPreference = "Stop"
$rp = $null
$ticket = $null
try {
    $rp = New-Object -ComObject QBXMLRP2.RequestProcessor
    $rp.OpenConnection2("", $AppName, 1)            # 1 = local QuickBooks Desktop
    # Always pass the path: attaches to an instance that has this file open,
    # or launches QuickBooks with it.
    $ticket = $rp.BeginSession($CompanyFile, 2)     # 2 = open mode "do not care"
    $out = $rp.ProcessRequest($ticket, [IO.File]::ReadAllText($Request))
    [IO.File]::WriteAllText($Response, $out, (New-Object Text.UTF8Encoding $false))
    Set-Content -Path $Status -Value "OK" -Encoding ASCII
} catch {
    Set-Content -Path $Status -Value ("ERR: " + $_.Exception.Message) -Encoding ASCII
} finally {
    if ($rp) {
        if ($ticket) { try { $rp.EndSession($ticket) } catch {} }
        try { $rp.CloseConnection() } catch {}
    }
}
