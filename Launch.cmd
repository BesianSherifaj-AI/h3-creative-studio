@echo off
cd /d "%~dp0"
if not exist ".venv\.h3-ready" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup.ps1"
  if errorlevel 1 exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0studio.py" %*
