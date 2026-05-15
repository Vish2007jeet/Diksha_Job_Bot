@echo off
cd /d D:\Job_Bot

echo.
echo ================================================================
echo   Job Bot -- Email Monitor
echo   Reads Gmail, classifies with Haiku, sends Telegram cards
echo ================================================================
echo.
echo Options:
echo   [1] Process new unread emails only  (default)
echo   [2] Back-fill: scan recent 200 emails (first-time setup)
echo   [3] Dry run: classify + print, no Telegram send
echo   [4] Force reprocess all (even already-seen emails)
echo.
set /p CHOICE="Choose [1-4] or press Enter for default (1): "
if "%CHOICE%"=="" set CHOICE=1

if "%CHOICE%"=="1" (
    echo.
    echo Running: unread emails only...
    .venv\Scripts\python email_monitor.py
)
if "%CHOICE%"=="2" (
    echo.
    echo Running: back-fill mode (recent 200 emails)...
    .venv\Scripts\python email_monitor.py --all
)
if "%CHOICE%"=="3" (
    echo.
    echo Running: dry run (no Telegram, no DB writes)...
    .venv\Scripts\python email_monitor.py --dry
)
if "%CHOICE%"=="4" (
    echo.
    echo Running: force reprocess all...
    .venv\Scripts\python email_monitor.py --all --force
)

echo.
pause
