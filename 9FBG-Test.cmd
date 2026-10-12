@echo off
setlocal
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m pytest -q %*
exit /b %ERRORLEVEL%
