@echo off
echo Installing Job Bot as a Windows Service...
echo.

set NSSM=D:\Job_Bot\tools\nssm.exe
set PYTHON=D:\Job_Bot\.venv\Scripts\python.exe
set SCRIPT=D:\Job_Bot\main.py
set LOGDIR=D:\Job_Bot\data\logs

:: Create log directory
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

:: Remove old service if it exists
%NSSM% stop JobBot 2>nul
%NSSM% remove JobBot confirm 2>nul

:: Install service
%NSSM% install JobBot "%PYTHON%" "%SCRIPT%"

:: Set working directory (important for relative paths in config)
%NSSM% set JobBot AppDirectory D:\Job_Bot

:: Redirect stdout and stderr to log files
%NSSM% set JobBot AppStdout "%LOGDIR%\jobbot.log"
%NSSM% set JobBot AppStderr "%LOGDIR%\jobbot_error.log"

:: Rotate logs at 10MB
%NSSM% set JobBot AppRotateFiles 1
%NSSM% set JobBot AppRotateBytes 10485760

:: Restart automatically if it crashes
%NSSM% set JobBot AppRestartDelay 5000

:: Set startup type to Automatic (starts on Windows boot)
%NSSM% set JobBot Start SERVICE_AUTO_START

:: Start the service now
%NSSM% start JobBot

echo.
echo ============================================
echo  Job Bot service installed and started!
echo  It will now start automatically on boot.
echo.
echo  Check status:   tools\nssm.exe status JobBot
echo  Stop service:   tools\nssm.exe stop JobBot
echo  Start service:  tools\nssm.exe start JobBot
echo  Remove service: tools\nssm.exe remove JobBot confirm
echo  View logs:      data\logs\jobbot.log
echo ============================================
echo.
pause
