@echo off
cd /d "%~dp0"
python -u run.py %*
if errorlevel 1 pause
