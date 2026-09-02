<#
Registers the webhook health-check as a repeating Scheduled Task.

The task is the watchdog; the script it runs is one-shot. That inversion is deliberate —
the earlier design used a long-lived supervisor loop, which died with exit code 0xC000013A
(STATUS_CONTROL_C_EXIT) when its console closed and took the endpoint down silently for
hours. Task Scheduler is the thing on Windows that is already supervised, so it owns the
lifetime and the script just does one check-and-repair pass.

Triggers on logon, then repeats every 5 minutes.

Install:   powershell -ExecutionPolicy Bypass -File scripts\install_webhook_task.ps1
Remove:    powershell -ExecutionPolicy Bypass -File scripts\install_webhook_task.ps1 -Uninstall
Status:    Get-ScheduledTask -TaskName RazorpayWebhookDaemon | Get-ScheduledTaskInfo
#>
param(
    [switch]$Uninstall,
    [string]$TaskName = "RazorpayWebhookDaemon",
    [int]$EveryMinutes = 5
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Daemon = Join-Path $Repo "scripts\webhook_daemon.ps1"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "removed scheduled task '$TaskName'"
    } else {
        Write-Output "task '$TaskName' was not registered"
    }
    return
}

if (-not (Test-Path $Daemon)) { throw "daemon script not found at $Daemon" }

# -NonInteractive and -WindowStyle Hidden keep it off any console. -File (not -Command)
# avoids quoting surprises in the scheduled-task argument string.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Daemon`"" `
    -WorkingDirectory $Repo

$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# NOTE: an -AtStartup trigger needs elevation (registering it non-elevated fails with
# 0x80070005 E_ACCESSDENIED), because it runs before any user logs on. We deliberately
# stay non-elevated, so the endpoint is down between boot and logon. The heartbeat
# picks it up within $EveryMinutes of you signing in.

# Repetition is the actual watchdog: even if a run dies, the next one is minutes away.
# It lives on its own -Once trigger rather than being grafted onto the logon/startup ones,
# because assigning .Repetition across trigger objects is fragile, and
# [TimeSpan]::MaxValue as a duration overflows the scheduler (SCHED_E_INVALIDVALUE
# 0x80041318). Ten years is indefinite enough for a hackathon submission.
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# A single pass should take seconds; cap it so a wedged run cannot block the next.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $heartbeat) `
    -Settings $settings -Principal $principal `
    -Description "Health-checks and repairs the Razorpay test-mode webhook endpoint every $EveryMinutes minutes." | Out-Null

Write-Output "registered '$TaskName' - repeats every $EveryMinutes minute(s) after logon"
Write-Output "log: $Repo\runtime\webhook-daemon.log (quiet when healthy)"
