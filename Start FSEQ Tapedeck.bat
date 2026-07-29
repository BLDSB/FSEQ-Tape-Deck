@echo off
cd /d "%~dp0fseq_tapedeck"

echo Starting FSEQ Tapedeck server...
start "FSEQ Tapedeck Server" cmd /k python main.py

timeout /t 2 /nobreak >nul
start "" http://localhost:7979
