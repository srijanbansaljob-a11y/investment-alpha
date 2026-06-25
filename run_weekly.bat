@echo off
REM ============================================================
REM  Investment Alpha - Weekly Rebalance Runner (DRY / analysis only)
REM  Places NO trades. Use to preview the week's target portfolio.
REM  Double-click any time, or schedule with Windows Task Scheduler.
REM ============================================================
setlocal

set PROJECT_DIR=%~dp0

REM -- Force Python to recompile from source (bypasses stale .pyc files)
set PYTHONPYCACHEPREFIX=%TEMP%\investment_alpha_pycache

REM -- Python executable (full path to avoid wrong interpreter being picked up)
set PYTHON=C:\Users\srija\AppData\Local\Python\pythoncore-3.14-64\python.exe

REM -- Activate virtual environment if present
if exist "%PROJECT_DIR%venv\Scripts\activate.bat" (
    call "%PROJECT_DIR%venv\Scripts\activate.bat"
)

echo.
echo ============================================================
echo   INVESTMENT ALPHA - WEEKLY REBALANCE (DRY RUN)
echo   %DATE% %TIME%
echo ============================================================
echo.

"%PYTHON%" "%PROJECT_DIR%main.py"

echo.
echo ============================================================
echo   Run complete. Check outputs\ folder for results.
echo ============================================================
echo.

endlocal
pause
