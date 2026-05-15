@echo off
cd /d D:\Job_Bot

echo.
echo ================================================================
echo   Job Bot -- Health Check ^& Authorization Verifier
echo ================================================================
echo.

echo [1/2] Installing / updating dependencies...
.venv\Scripts\pip install -q google-api-python-client google-auth-httplib2 google-auth-oauthlib
if errorlevel 1 (
    echo ERROR: pip install failed. Is the venv set up?
    pause
    exit /b 1
)
echo Done.
echo.

echo [2/2] Running health check...
echo.
.venv\Scripts\python healthcheck.py %*

echo.
pause
