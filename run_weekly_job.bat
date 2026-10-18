@echo off
REM Weekly job for Windows Task Scheduler. Runs the whole pipeline and writes to logs\task_scheduler.log
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" reporting\run_weekly_job.py >> logs\task_scheduler.log 2>&1
exit /b %ERRORLEVEL%
