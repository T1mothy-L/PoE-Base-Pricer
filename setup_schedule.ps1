# Registers (or re-registers) the price tracker as a Windows Scheduled Task.
# Run ONCE in an elevated PowerShell prompt sitting in this repo. Re-run any
# time to change the schedule.
#
# Defaults: every 4 hours, wakes laptop from sleep, runs only when plugged
# in. Adjust the variables below to taste.

$ErrorActionPreference = 'Stop'

# ---- knobs ----------------------------------------------------------------
$taskName        = 'PoE2 Price Tracker'
$intervalHours   = 3              # how often to run
$firstRunAt      = '01:00'        # daily anchor; runs at this time + every N hours after
$timeoutHours    = 2           # hard kill if a run takes longer (80 items ≈ 35 min)
# ---------------------------------------------------------------------------

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$wrapperPath = Join-Path $scriptDir 'run_tracker.ps1'

if (-not (Test-Path $wrapperPath)) {
    throw "run_tracker.ps1 not found at $wrapperPath"
}

# Trigger: daily at $firstRunAt, then repeating every $intervalHours through the day.
$trigger = New-ScheduledTaskTrigger -Daily -At $firstRunAt
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $firstRunAt `
    -RepetitionInterval (New-TimeSpan -Hours $intervalHours) `
    -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapperPath`"" `
    -WorkingDirectory $scriptDir

# WakeToRun wakes a sleeping laptop (lid closed) to run the task. Defaults
# keep it from running on battery so a closed laptop doesn't drain itself
# overnight; flip -AllowStartIfOnBatteries / -DontStopIfGoingOnBatteries
# if you want it to run on battery too.
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours $timeoutHours) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Trigger $trigger `
    -Action $action `
    -Settings $settings `
    -Principal $principal `
    -Description 'PoE2 white-base price tracker. Runs every few hours and pushes prices.db + latest.json to GitHub on success.' | Out-Null

Write-Host "Registered '$taskName'. Upcoming runs:"
(Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo).NextRunTime
Write-Host ""
Write-Host "To run it once manually right now:"
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
Write-Host ""
Write-Host "To remove the schedule later:"
Write-Host "  Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
