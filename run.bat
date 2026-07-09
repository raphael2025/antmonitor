@echo off
rem 守护循环：服务退出(含更新器自动重启的 exit 42)后 3 秒自动用新代码重新拉起。
rem 生产更推荐 NSSM 注册为 Windows 服务(开机自启)；本脚本适合快速部署/调试。
cd /d %~dp0
:loop
python server.py
echo [%date% %time%] server exited, restart in 3s...
timeout /t 3 /nobreak >nul
goto loop
