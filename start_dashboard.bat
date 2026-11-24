@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create the virtual environment and install requirements-dashboard.txt first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run dashboard.py --server.address localhost
if errorlevel 1 pause
