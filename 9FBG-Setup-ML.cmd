@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 请先运行 9FBG-Setup.cmd 创建独立环境。
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-ml.txt"
exit /b %ERRORLEVEL%
