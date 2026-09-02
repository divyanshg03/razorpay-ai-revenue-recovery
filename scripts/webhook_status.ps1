<#
One-second health check for the Razorpay webhook endpoint.

Use this instead of Test-NetConnection, which spends ~20s on DNS, ping and route lookups
and prints "Attempting TCP connect / Waiting for response" progress bars that look like a
hang even when the port is fine.

The authoritative check is the PUBLIC PROBE, not the local process list. A previous version
of this script reported healthy while the endpoint was returning 502: the receiver was
listening and zrok2 was running, but the tunnel was not serving. Only an end-to-end probe
catches that, so that is what decides the verdict here.

The probe sends an UNSIGNED request and expects 400 "invalid signature" — which proves the
whole path works while creating no event, so running this never pollutes the evidence in
results/phase0/0.4c-received-events.jsonl.

    powershell -ExecutionPolicy Bypass -File scripts\webhook_status.ps1
#>
param(
    [int]$Port = 9090,
    [string]$Url = "https://<zrok-share-name>.shares.zrok.io/webhook",
    [string]$TaskName = "RazorpayWebhookDaemon"
)

$Repo = Split-Path -Parent $PSScriptRoot
$EventLog = Join-Path $Repo "results\phase0\0.4c-received-events.jsonl"

function Test-Port([int]$p) {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $p); return $true }
    catch { return $false }
    finally { $client.Dispose() }
}

function Get-PublicStatus {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "POST"; $req.Timeout = 25000; $req.ContentType = "application/json"
        $bytes = [Text.Encoding]::UTF8.GetBytes("{}")
        $req.ContentLength = $bytes.Length
        $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
        $resp = $req.GetResponse(); $code = [int]$resp.StatusCode; $resp.Close()
        return $code
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    } catch { return 0 }
}

$listening = Test-Port $Port
$zrok = [bool](Get-Process -Name zrok2 -ErrorAction SilentlyContinue)
$probe = Get-PublicStatus
$healthy = ($probe -eq 400)

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $info = $task | Get-ScheduledTaskInfo
    # The task is one-shot and repeats, so "Ready" is the NORMAL resting state - it means
    # the last pass finished and the next tick is scheduled. "Running" just means a pass
    # happens to be mid-flight. Neither is a fault; LastTaskResult is what matters.
    $taskLine = "{0}  (last result {1}, next run {2})" -f $task.State, $info.LastTaskResult, $info.NextRunTime
} else {
    $taskLine = "NOT INSTALLED"
}

Write-Output ""
if ($healthy) {
    Write-Output "  ENDPOINT HEALTHY"
} else {
    Write-Output "  ENDPOINT DOWN  (public probe returned $probe, expected 400)"
}
Write-Output ("  public    : {0}  -> {1}" -f $Url, $probe)
Write-Output ("  receiver  : " + $(if ($listening) { "UP   (port $Port listening)" } else { "DOWN (port $Port closed)" }))
Write-Output ("  zrok      : " + $(if ($zrok) { "UP" } else { "DOWN" }))
Write-Output ("  watchdog  : $taskLine")

if (Test-Path $EventLog) {
    $lines = @(Get-Content $EventLog)
    Write-Output ("  events    : {0} verified event(s) logged" -f $lines.Count)
    if ($lines.Count -gt 0) {
        $last = $lines[-1] | ConvertFrom-Json
        Write-Output ("  last event: {0}  {1}" -f $last.received_at, $last.event)
    }
} else {
    Write-Output "  events    : none logged yet"
}

if (-not $healthy) {
    Write-Output ""
    Write-Output "  Razorpay retries for 24h then AUTO-DISABLES the webhook."
    Write-Output "  The watchdog repairs this within ~5 minutes. To force it now:"
    Write-Output "    powershell -ExecutionPolicy Bypass -File scripts\webhook_daemon.ps1"
    Write-Output "    Get-Content runtime\webhook-daemon.log -Tail 20"
}
Write-Output ""
