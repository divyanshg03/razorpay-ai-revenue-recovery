<#
Keeps the Razorpay webhook endpoint alive. Runs ONE-SHOT: check, repair, exit.

Razorpay retries a failing webhook with exponential backoff for 24 hours and then
AUTO-DISABLES it, so an endpoint that fails silently is worse than none at all.

DESIGN NOTE — why this is one-shot and not a supervisor loop.

The first version was an infinite `while ($true)` loop. It died within seconds of every
start with exit code 0xC000013A (STATUS_CONTROL_C_EXIT) — the console it was attached to
closed and took it with it. A long-lived supervisor is itself a single point of failure,
and "who supervises the supervisor" has a standard answer on Windows: Task Scheduler.
So this script now does one pass and exits, and the task repeats it every few minutes.
Nothing long-lived means nothing to kill.

SECOND DESIGN NOTE — why the health check probes the public URL.

The first version checked "is there a zrok2 process". That passed while the endpoint was
returning 502: the process was alive, the share record existed, and the tunnel still was
not serving. Only an end-to-end probe catches that.

The probe sends an UNSIGNED request and expects HTTP 400 "invalid signature". A 400 proves
the whole path works — tunnel reaches receiver, receiver runs the handler — while creating
no event, so health checks never pollute the evidence in
results/phase0/0.4c-received-events.jsonl. Anything other than 400 means broken.

    powershell -ExecutionPolicy Bypass -File scripts\webhook_daemon.ps1
#>
param(
    [int]$Port = 9090,
    # Read from .env (gitignored), never hardcoded: this repo is destined to be public and
    # git history is permanent. Pass -Secret to override.
    [string]$Secret = "",
    [string]$ZrokName = "<zrok-share-name>",
    [string]$Namespace = "public",
    [string]$ShareDomain = "shares.zrok.io",
    # Pinned deliberately. Launching "python" by name resolves via PATH, which on this
    # machine lands in an unrelated tool's virtualenv. If that venv moves
    # or PATH differs when the task runs, the receiver silently fails to start.
    [string]$Python = "C:\Users\Divyansh\AppData\Local\Programs\Python\Python313\python.exe"
)

$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

$Zrok = Join-Path $Repo "tools\zrok\zrok2.exe"
$RuntimeDir = Join-Path $Repo "runtime"
$LogFile = Join-Path $RuntimeDir "webhook-daemon.log"
$EventLog = Join-Path $Repo "results\phase0\0.4c-received-events.jsonl"
# Rejections go to runtime/, not results/: a public endpoint attracts internet scanners and
# that noise does not belong in committed evidence. But they MUST be recorded somewhere, or
# a wrong secret in the Dashboard is indistinguishable from Razorpay never calling at all —
# both look like an empty event log behind a perfectly healthy endpoint.
$RejectLog = Join-Path $RuntimeDir "webhook-rejected.jsonl"
$PublicUrl = "https://$ZrokName.$ShareDomain/webhook"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Get-EnvValue([string]$Name) {
    $envFile = Join-Path $Repo ".env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if ($trimmed -and -not $trimmed.StartsWith("#") -and $trimmed.Contains("=")) {
            $parts = $trimmed.Split("=", 2)
            # .Trim() on the VALUE is deliberate. Trailing whitespace on this exact secret
            # silently rejected 18 real Razorpay deliveries; never let it through again.
            if ($parts[0].Trim() -eq $Name) { return $parts[1].Trim() }
        }
    }
    return $null
}

if (-not $Secret) { $Secret = Get-EnvValue "RAZORPAY_WEBHOOK_SECRET" }
if (-not $Secret) {
    # Serving with an empty secret would reject every delivery while looking healthy -
    # exactly the silent failure this whole daemon exists to prevent.
    Add-Content -Path (Join-Path $RuntimeDir "webhook-daemon.log") -Encoding utf8 `
        -Value ("{0}  FATAL: RAZORPAY_WEBHOOK_SECRET not set in .env" -f (Get-Date -Format o))
    exit 1
}

function Write-Log([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Test-Port([int]$p) {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $p); return $true }
    catch { return $false }
    finally { $client.Dispose() }
}

function Get-PublicStatus {
    # Unsigned POST. Healthy == 400 (receiver rejected the signature), which proves the
    # full path without logging an event. 0 means no HTTP response at all.
    try {
        $req = [System.Net.HttpWebRequest]::Create($PublicUrl)
        $req.Method = "POST"
        $req.Timeout = 25000
        $req.ContentType = "application/json"
        $bytes = [Text.Encoding]::UTF8.GetBytes("{}")
        $req.ContentLength = $bytes.Length
        $stream = $req.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return $code
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    } catch {
        return 0
    }
}

function Start-Receiver {
    if (-not (Test-Path $Python)) {
        Write-Log "FATAL: interpreter not found at $Python - receiver cannot start"
        return
    }
    Write-Log "receiver down on port $Port - starting"
    $procArgs = @(
        (Join-Path $Repo "spikes\webhook_receiver.py"),
        "--port", $Port, "--secret", $Secret, "--log", $EventLog, "--reject-log", $RejectLog
    )
    Start-Process -FilePath $Python -ArgumentList $procArgs -WorkingDirectory $Repo `
        -WindowStyle Hidden
}

function Get-BoundShareToken {
    $raw = & $Zrok list shares --json
    if (-not $raw) { return $null }
    try { $parsed = ($raw | Out-String) | ConvertFrom-Json } catch { return $null }
    foreach ($share in $parsed.shares) {
        foreach ($endpoint in $share.frontendEndpoints) {
            if ($endpoint -like "$ZrokName.*") { return $share.shareToken }
        }
    }
    return $null
}

function Restart-Zrok {
    Write-Log "restarting zrok share"
    Get-Process -Name "zrok2" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    # The share record outlives the process serving it; leaving it causes 409 on recreate.
    $stale = Get-BoundShareToken
    if ($stale) {
        Write-Log "deleting stale share $stale"
        & $Zrok delete share $stale | Out-Null
        Start-Sleep -Seconds 3
    }
    $procArgs = @("share", "public", $Port, "--headless", "--force-local",
                  "-n", "$Namespace`:$ZrokName")
    Start-Process -FilePath $Zrok -ArgumentList $procArgs -WorkingDirectory $Repo `
        -WindowStyle Hidden
    Start-Sleep -Seconds 20   # share registration is not instant; 404 for ~12s otherwise
}

# ---- one pass ------------------------------------------------------------------------

if (-not (Test-Port $Port)) {
    Start-Receiver
    Start-Sleep -Seconds 4
}

$status = Get-PublicStatus
if ($status -eq 400) {
    exit 0   # healthy, and quiet: no log line, so the log only ever shows real events
}

Write-Log "UNHEALTHY: public probe returned $status (expected 400) - repairing"
Restart-Zrok

$status = Get-PublicStatus
if ($status -eq 400) {
    Write-Log "repaired: public probe now returns 400 as expected"
    exit 0
}

Write-Log "STILL UNHEALTHY after repair: probe returned $status"
exit 1
