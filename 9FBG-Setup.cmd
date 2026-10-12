@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" python -m venv "%~dp0.venv"
if errorlevel 1 exit /b %ERRORLEVEL%
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
exit /b %ERRORLEVEL%
