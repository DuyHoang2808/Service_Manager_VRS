@echo off
REM Mo Service Manager bang python cua Camera_venv (khong hien cua so console)
cd /d "%~dp0"
start "" "D:\Camera\Dev\Camera_venv\Scripts\pythonw.exe" "%~dp0service_manager.py"
