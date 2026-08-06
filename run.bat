@echo off
rem Guardian loop: restart server.py after it exits (incl. exit 42 = updater self-restart).
rem NSSM as a Windows service is preferred in production; this script suits quick deploy/debug.
rem
rem Circuit breaker: MAXFAILS consecutive "died within MINUP seconds" runs stop the loop,
rem so a broken build can no longer cause an endless crash-3s-crash restart storm.
rem   - exit code 42 = updater restarting on purpose: never counted, resets the counter
rem   - a run lasting MINUP seconds or more: the service was really up, resets the counter
rem
rem KEEP THIS FILE ASCII-ONLY AND CRLF. Non-ASCII text or LF-only line endings make cmd
rem mis-parse the set /a lines and the goto labels (verified: the loop then breaks apart).
setlocal
cd /d %~dp0

set MAXFAILS=5
set MINUP=30
set DELAY=3
set FAILS=0

:loop
set RUN=0
set "T=%TIME: =0%"
rem 1<hh>-100 keeps set /a from reading 08/09 as octal; %TIME: =0% pads a leading-space hour
set /a T1=(1%T:~0,2%-100)*3600+(1%T:~3,2%-100)*60+(1%T:~6,2%-100)

python server.py
set EC=%ERRORLEVEL%

set "T=%TIME: =0%"
set /a T2=(1%T:~0,2%-100)*3600+(1%T:~3,2%-100)*60+(1%T:~6,2%-100)
set /a RUN=T2-T1
if %RUN% lss 0 set /a RUN=RUN+86400

if "%EC%"=="42" goto healthy
if %RUN% geq %MINUP% goto healthy

set /a FAILS=FAILS+1
echo [%DATE% %TIME%] server exited (code=%EC%) after only %RUN%s - quick failure %FAILS%/%MAXFAILS%
if %FAILS% geq %MAXFAILS% goto circuit_break
echo     restarting in %DELAY%s...
call :sleep
goto loop

:healthy
set FAILS=0
echo [%DATE% %TIME%] server exited (code=%EC%) after %RUN%s, restarting in %DELAY%s...
call :sleep
goto loop

:sleep
rem timeout needs a real console (fails under NSSM/redirected stdin); ping is the fallback
timeout /t %DELAY% /nobreak >nul 2>&1 || ping -n %DELAY% 127.0.0.1 >nul 2>&1
goto :eof

:circuit_break
echo.
echo ============================================================
echo  server crashed %MAXFAILS% times in a row, each within %MINUP%s.
echo  AUTO-RESTART STOPPED - manual check required, do not just relaunch:
echo    1) read the traceback above and logs\miner.log
echo    2) python updater.py check     (which version is deployed / just updated?)
echo    3) if a bad update caused it:  git reset --hard ^<previous-commit^>
echo ============================================================
echo.
exit /b 1
