@echo off
setlocal enabledelayedexpansion
title Diksha Job Bot
cd /d "D:\Diksha_Job_Bot"

echo.
echo  Syncing to GitHub...
git add .
git diff --cached --quiet
if !errorlevel! equ 0 (
    echo  No changes to commit.
) else (
    git commit -m "Auto-sync: %date% %time%"
    git push origin main
    if !errorlevel! equ 0 (
        echo  Pushed to GitHub successfully.
    ) else (
        echo  WARNING: Git push failed. Starting bot anyway...
    )
)
echo.
echo  Starting Diksha Job Bot...
echo  Press Ctrl+C to stop.
echo.
python main.py
echo.
echo  Bot stopped.
pause
