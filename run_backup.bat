@echo off
REM Daily backup with restore test for Windows Task Scheduler. Output goes to logs\backup_task.log
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" backup\backup_database.py >> logs\backup_task.log 2>&1
exit /b %ERRORLEVEL%
