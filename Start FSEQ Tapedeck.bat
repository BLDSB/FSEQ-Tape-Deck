@echo off
cd /d "%~dp0fseq_tapedeck"

rem Prefer the project's own virtualenv. A bare "python" on Windows often
rem resolves to the Microsoft Store stub, which has none of the dependencies
rem installed and fails with "No module named uvicorn".
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo Starting FSEQ Tapedeck server...
start "FSEQ Tapedeck Server" cmd /k "%PYTHON%" main.py

timeout /t 2 /nobreak >nul
start "" http://localhost:7979
