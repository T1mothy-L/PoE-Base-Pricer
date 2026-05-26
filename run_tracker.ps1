# Wrapper for Task Scheduler. Runs the price tracker, and on a clean exit
# (0) stages, commits, and pushes the updated prices.db + latest.json so
# the GitHub repo stays in sync. On a non-zero exit it does nothing —
# poe2_price_tracker.py already sent a Telegram ping with the failure
# reason.
#
# This script is meant to be invoked by the scheduled task that
# setup_schedule.ps1 registers. Run it manually any time to do a one-off
# refresh.

$ErrorActionPreference = 'Continue'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Log($msg) {
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
}

Log "Starting tracker in $scriptDir"
& python poe2_price_tracker.py
$exitCode = $LASTEXITCODE
Log "Tracker exited $exitCode"

if ($exitCode -ne 0) {
    Log "Non-zero exit; skipping commit/push (tracker already pinged Telegram)."
    exit $exitCode
}

# Stage only the data files. Avoid `git add .` so any accidental local
# edits don't sneak into a 'prices:' commit.
git add prices.db latest.json
$changes = git status --porcelain prices.db latest.json
if (-not $changes) {
    Log "No data changes to commit."
    exit 0
}

$ts = Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'
git commit -m "prices: $ts"
if ($LASTEXITCODE -ne 0) {
    Log "git commit failed."
    exit 1
}

git push
if ($LASTEXITCODE -ne 0) {
    Log "git push failed (local commit was made; will be picked up on next push)."
    exit 1
}

Log "Pushed: prices: $ts"
exit 0
