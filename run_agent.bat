@echo off
cd /d "%~dp0"
echo [%date% %time%] Starting Zoho Projects Agent...
C:\Python314\python.exe main.py >> logsgent.log 2>&1
echo [%date% %time%] Done.
