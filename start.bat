@echo off
setlocal enabledelayedexpansion
cd /d D:\Job_Bot

echo.
echo ============================================================
echo   Job Bot - Git Sync + Start
echo ============================================================

:: ── 0. Kill any existing bot instance ────────────────────────
echo.
echo [0/4] Stopping any existing bot instance...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1
echo      Done.

:: ── 0.5 Merge any pending Claude worktree branches ───────────
echo.
echo [0.5/4] Merging pending Claude branches...
for /f "tokens=*" %%b in ('git branch ^| findstr "claude/"') do (
    git merge --no-edit %%b >nul 2>&1
    if !errorlevel! equ 0 (
        echo      Merged: %%b
    ) else (
        echo      Skipped: %%b (already merged or conflict^)
    )
)
echo      Done.

:: ── 1. Stage all changes ─────────────────────────────────────
echo.
echo [1/4] Staging all changes...
git add -A
if %errorlevel% neq 0 (
    echo      ERROR: git add failed.
    pause
    exit /b 1
)

:: ── 2. Commit (skip if nothing to commit) ────────────────────
echo.
echo [2/4] Committing...
git diff --cached --quiet
if %errorlevel% equ 0 goto :nothing_to_commit

set TIMESTAMP=%date% %time:~0,5%
git commit -m "auto: local changes saved on %TIMESTAMP%"
if %errorlevel% neq 0 (
    echo      ERROR: git commit failed.
    pause
    exit /b 1
)
echo      Committed.
goto :push

:nothing_to_commit
echo      Nothing to commit - working tree clean.

:: ── 3. Push to GitHub ─────────────────────────────────────────
:push
echo.
echo [3/4] Pushing to GitHub...
git push origin main
if %errorlevel% neq 0 (
    echo      WARNING: git push failed. Bot will still start.
    echo      Push manually later: git push origin main
) else (
    echo      GitHub is up to date.
)

:: ── 4. Start the bot ──────────────────────────────────────────
echo.
echo [4/4] Starting Job Bot...
echo.
echo ============================================================
echo   Bot is running. Close this window to stop it.
echo ============================================================
echo.
.venv\Scripts\python.exe main.py
echo.
echo Bot exited with code %errorlevel%.
pause
