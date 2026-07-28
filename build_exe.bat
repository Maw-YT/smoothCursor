@echo off
cd /d "%~dp0"
echo Building SmoothCursor.exe with PyInstaller...
python -m pip install -r requirements.txt
python build_exe.py
if errorlevel 1 pause
echo.
echo Output: dist\SmoothCursor.exe
pause
