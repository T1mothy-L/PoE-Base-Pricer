# Registers (or re-registers) the price tracker as a Windows Scheduled Task.
# Run ONCE in an elevated PowerShell prompt sitting in this repo. Re-run any
# time to change the schedule.
#
# The task is registered to "run whether you are logged on or not" (background,
# no console window) via an S4U logon, so it fires with the lid closed during
# Modern Standby without waiting for you to log in -- and needs NO stored
# password (it works even with Windows Hello-only sign-in enforced). Because an
# S4U task can't read Credential Manager, git push uses an SSH deploy key; see
# the README "Running on a schedule" section for the one-time SSH setup.
#
# Defaults: every couple of hours, runs only when plugged in. Adjust the
# variables below to taste.

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

# On a Modern Standby (S0) laptop the OS keeps running with the lid closed, so a
# background task fires on its own during standby -- WakeToRun is a harmless no-op
# there (it only matters on older S3-sleep machines), and StartWhenAvailable
# catches a run missed while the machine was fully off/hibernated. Defaults keep
# the task from running on battery so a closed laptop doesn't drain itself; flip
# -AllowStartIfOnBatteries / -DontStopIfGoingOnBatteries to run on battery too
# (unreliable during Modern Standby off AC).
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours $timeoutHours) `
    -MultipleInstances IgnoreNew

# S4U ("Service For User") runs the task whether or not you're logged on WITHOUT
# storing a password, so it's unaffected by the Windows Hello-only sign-in
# setting. The trade-off -- no Credential Manager access -- is handled by pushing
# over SSH with a passphrase-less deploy key (one-time setup in the README).
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
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
