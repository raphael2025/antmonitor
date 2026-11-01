@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto missing_python
echo Paste the connection information copied from the cloud. Input is hidden.
echo Close the original monitor first if you want this command to start it again.
".venv\Scripts\python.exe" -X utf8 "cloud_connect.py" --start
set "CONNECT_RESULT=%ERRORLEVEL%"
if "%CONNECT_RESULT%"=="0" goto complete
if "%CONNECT_RESULT%"=="10" goto complete
if "%CONNECT_RESULT%"=="20" goto complete
echo Connection was not saved. Check the message above and try again.
goto complete
:missing_python
echo This folder has no working AntMonitor environment yet. Run run.bat to finish installation first.
set "CONNECT_RESULT=1"
:complete
if not defined MINER_NO_PAUSE pause
exit /b %CONNECT_RESULT%
