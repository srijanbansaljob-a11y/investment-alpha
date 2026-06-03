@echo off
REM ============================================================
REM  Investment Alpha - Weekly Stop-Loss Check
REM  Run every Monday morning before market open (9:00 AM ET)
REM  Schedule with Windows Task Scheduler
REM ============================================================
setlocal

set PROJECT_DIR=%~dp0
set PYTHONPYCACHEPREFIX=%TEMP%\investment_alpha_pycache
set PYTHON=C:\Users\srija\AppData\Local\Python\pythoncore-3.14-64\python.exe

if exist "%PROJECT_DIR%venv\Scripts\activate.bat" (
    call "%PROJECT_DIR%venv\Scripts\activate.bat"
)

echo.
echo ============================================================
echo   INVESTMENT ALPHA - WEEKLY STOP-LOSS CHECK
echo   %DATE% %TIME%
echo ============================================================
echo.

cd /d "%PROJECT_DIR%"
"%PYTHON%" "%PROJECT_DIR%broker\stop_loss.py"
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% EQU 0 (
    echo Stop-loss check complete. See outputs\stop_loss_log.json
) else (
    echo ERROR: Stop-loss script failed with exit code %EXITCODE%
    echo Check the output above for the Python traceback.
)
echo.

endlocal
