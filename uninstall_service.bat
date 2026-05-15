@echo off
echo Stopping and removing Job Bot service...
D:\Job_Bot\tools\nssm.exe stop JobBot
D:\Job_Bot\tools\nssm.exe remove JobBot confirm
echo Done.
pause
