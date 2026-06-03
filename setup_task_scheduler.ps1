# ============================================================
#  Investment Alpha - Windows Task Scheduler Setup
#  Run this script ONCE as Administrator to register tasks
#  Right-click this file -> "Run with PowerShell" (as Admin)
# ============================================================

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe  = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    Write-Error "Python not found in PATH. Install Python and try again."
    exit 1
}

# Force fresh source compilation (bypasses stale Windows .pyc files)
$EnvVars = @("PYTHONPYCACHEPREFIX=$env:TEMP\investment_alpha_pycache")

Write-Host ""
Write-Host "=================================================="
Write-Host "  Investment Alpha - Task Scheduler Setup"
Write-Host "  Project : $ProjectDir"
Write-Host "  Python  : $PythonExe"
Write-Host "=================================================="
Write-Host ""

# --- Task 1: Monthly Rebalance (1st of each month, 8:30 AM) ---
$TaskName1 = "InvestmentAlpha_MonthlyRebalance"
$Trigger1  = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At "08:30AM"
$Action1   = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ProjectDir\main.py`"" `
    -WorkingDirectory $ProjectDir
$Settings1 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RunOnlyIfNetworkAvailable `
    -StartWhenAvailable

$Env1 = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest

Register-ScheduledTask `
    -TaskName   $TaskName1 `
    -Trigger    $Trigger1 `
    -Action     $Action1 `
    -Settings   $Settings1 `
    -Description "Investment Alpha: monthly pipeline run (analysis only, no trade execution)" `
    -Force | Out-Null

Write-Host "  [OK] $TaskName1  ->  1st of each month at 8:30 AM"

# --- Task 2: Weekly Stop-Loss Check (every Monday, 9:00 AM) ---
$TaskName2 = "InvestmentAlpha_WeeklyStopLoss"
$Trigger2  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "09:00AM"
$Action2   = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ProjectDir\broker\stop_loss.py`"" `
    -WorkingDirectory $ProjectDir
$Settings2 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RunOnlyIfNetworkAvailable `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName   $TaskName2 `
    -Trigger    $Trigger2 `
    -Action     $Action2 `
    -Settings   $Settings2 `
    -Description "Investment Alpha: weekly stop-loss check (dry run - review stop_loss_log.json)" `
    -Force | Out-Null

Write-Host "  [OK] $TaskName2  ->  Every Monday at 9:00 AM"
Write-Host ""
Write-Host "  To view tasks  : Open Task Scheduler -> Task Scheduler Library"
Write-Host "  To test now    : Right-click task -> Run"
Write-Host ""
Write-Host "  NOTE: Monthly task runs pipeline only (no trade execution)."
Write-Host "        To execute trades: run run_monthly_execute.bat manually."
Write-Host "=================================================="
