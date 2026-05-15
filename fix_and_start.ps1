# ============================================================
#  Job Bot - Fix launcher + install health scheduler
#  Run from PowerShell:
#    powershell -ExecutionPolicy Bypass -File .\fix_and_start.ps1
# ============================================================

$NSSM   = "D:\Job_Bot\tools\nssm.exe"
$PYTHON = "D:\Job_Bot\.venv\Scripts\python.exe"
$SCRIPT = "D:\Job_Bot\main.py"
$LOGDIR = "D:\Job_Bot\data\logs"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Job Bot - Fix and Start" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# -- 1. Verify venv Python exists ----------------------------
Write-Host ""
Write-Host "[1/4] Checking venv Python..." -NoNewline
if (Test-Path $PYTHON) {
    Write-Host " OK" -ForegroundColor Green
} else {
    Write-Host " NOT FOUND" -ForegroundColor Red
    Write-Host "      Expected: $PYTHON"
    exit 1
}

# -- 2. Fix NSSM service -------------------------------------
Write-Host ""
Write-Host "[2/4] Fixing NSSM service (JobBot)..." -NoNewline
if (-not (Test-Path $NSSM)) {
    Write-Host " NSSM not found at $NSSM" -ForegroundColor Red
    exit 1
}

& $NSSM stop JobBot 2>$null | Out-Null
& $NSSM remove JobBot confirm 2>$null | Out-Null

& $NSSM install JobBot $PYTHON $SCRIPT
& $NSSM set JobBot AppDirectory "D:\Job_Bot"
& $NSSM set JobBot AppStdout "$LOGDIR\jobbot.log"
& $NSSM set JobBot AppStderr "$LOGDIR\jobbot_error.log"
& $NSSM set JobBot AppRotateFiles 1
& $NSSM set JobBot AppRotateBytes 10485760
& $NSSM set JobBot AppRestartDelay 5000
& $NSSM set JobBot Start SERVICE_AUTO_START

Write-Host " Done" -ForegroundColor Green
Write-Host "      Python: $PYTHON" -ForegroundColor Gray

# -- 3. Start the service ------------------------------------
Write-Host ""
Write-Host "[3/4] Starting JobBot service..." -NoNewline
& $NSSM start JobBot
Start-Sleep -Seconds 3

$status = & $NSSM status JobBot 2>&1
if ($status -match "SERVICE_RUNNING") {
    Write-Host "      Status: $status" -ForegroundColor Green
} else {
    Write-Host "      Status: $status" -ForegroundColor Yellow
}

# -- 4. Install health alert scheduler -----------------------
Write-Host ""
Write-Host "[4/4] Installing JobBotHealthAlert scheduled task..." -NoNewline

$action   = New-ScheduledTaskAction -Execute $PYTHON -Argument "D:\Job_Bot\health_alert.py"
$trigger  = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 30) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)

Unregister-ScheduledTask -TaskName "JobBotHealthAlert" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "JobBotHealthAlert" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null

Write-Host " Done" -ForegroundColor Green
Write-Host "      Runs every 30 minutes using venv Python" -ForegroundColor Gray

# -- Summary -------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  All done!" -ForegroundColor Green
Write-Host "  Bot status : $status"
Write-Host "  Logs       : $LOGDIR\jobbot.log"
Write-Host "  Run health : schtasks /Run /TN JobBotHealthAlert"
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
