@echo off
REM ============================================================
REM  Investment Alpha - Monthly Rebalance WITH TRADE EXECUTION
REM  WARNING: This places real paper orders on Alpaca.
REM  Run only once per month on rebalance day.
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
echo   INVESTMENT ALPHA - MONTHLY REBALANCE + EXECUTION
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
