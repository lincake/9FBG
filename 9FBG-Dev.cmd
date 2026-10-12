@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 9FBG 运行环境不存在，请先运行 9FBG-Setup.cmd。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m ninefbg.server %*
set "NINEFBG_EXIT_CODE=%ERRORLEVEL%"
if not "%NINEFBG_EXIT_CODE%"=="0" (
  echo 9FBG 启动失败，请查看上方错误信息。
  pause
)
exit /b %NINEFBG_EXIT_CODE%
