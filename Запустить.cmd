@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "HHAssistant.exe" (
  start "" "HHAssistant.exe"
  exit /b
)
python launch.py
if errorlevel 1 pause
