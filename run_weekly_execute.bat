@echo off
REM ============================================================
REM  Investment Alpha - Weekly Rebalance WITH TRADE EXECUTION
REM  WARNING: This places real paper orders on Alpaca.
REM  This is the MANUAL, ad-hoc version (asks you to type YES).
REM  The automatic weekly run is registered by setup_task_scheduler.ps1
REM  (Monday 10:00 AM) and runs without a prompt.
REM
REM  Best run during US market hours (9:30 AM - 4:00 PM ET):
REM  Alpaca rejects fractional-share orders while the market is closed.
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
echo   INVESTMENT ALPHA - WEEKLY REBALANCE + EXECUTION
echo   %DATE% %TIME%
echo   WARNING: Will place real paper trades on Alpaca
echo ============================================================
echo.

set /p CONFIRM="Type YES to continue: "
if /i not "%CONFIRM%"=="YES" (
    echo Cancelled.
    goto :end
)

"%PYTHON%" "%PROJECT_DIR%main.py" --execute

echo.
echo ============================================================
echo   Execution complete. Check outputs\ folder for results.
echo ============================================================
echo.

:end
endlocal
pause
