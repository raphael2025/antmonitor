@echo off
goto bootstrap
rem AntMonitor Windows launcher. Keep this file ASCII and CRLF.
rem Legacy guardians resume at byte 1050 after python server.py exits.
rem Keep the handoff below at that offset so running old versions can update.
rem The Python guardian does not depend on batch read offsets for new updates.
rem -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
rem Legacy update handoff: start the new bootstrap in this same console.
goto bootstrap

:bootstrap
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || goto directory_error
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 goto start_venv

:find_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 goto start_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 goto start_python

echo Python 3.8 or newer was not found.
echo Install it from https://www.python.org/downloads/windows/
echo Enable "Add python.exe to PATH", then double-click run.bat again.
goto failed

:directory_error
echo Cannot open the AntMonitor folder. Extract the ZIP to a local writable folder.
goto failed

rem Keep each launch and exit on ONE parsed line. An update may replace run.bat
rem while Python runs; CMD must not read a changed file when the guardian exits.
:start_venv
".venv\Scripts\python.exe" "windows_launcher.py" & exit /b

:start_py
py -3 "windows_launcher.py" & exit /b

:start_python
python "windows_launcher.py" & exit /b

:failed
if not defined MINER_NO_PAUSE pause
exit /b 1
