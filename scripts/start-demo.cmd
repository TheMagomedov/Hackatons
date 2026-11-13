@echo off
setlocal
set "DEMO_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%DEMO_PYTHON%" (
  echo Missing project .venv. Follow docs/WINDOWS_DEMO.md first.
  exit /b 1
)
"%DEMO_PYTHON%" "%~dp0start_windows_dependencies.py" %*
exit /b %errorlevel%
