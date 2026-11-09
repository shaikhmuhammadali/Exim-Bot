@echo off
REM ============================================================
REM  EXIM Insight ^& Q^&A Bot  --  H.G. Hawa ^& Co.
REM  Double-click this file to start the bot, then open the
REM  address it prints (http://127.0.0.1:5000) in your browser.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Pick a Python interpreter (prefer the Windows 'py' launcher).
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo Could not find Python. Install Python 3 from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)

echo Checking dependencies...
%PYEXE% -c "import flask,pandas,openpyxl,numpy" 2>nul
if errorlevel 1 (
  echo Installing required packages (first run only)...
  %PYEXE% -m pip install -r requirements.txt
)

echo.
echo Starting the EXIM Insight ^& Q^&A Bot...
echo Open your browser at:  http://127.0.0.1:5000
echo (Close this window to stop the bot.)
echo.
%PYEXE% app.py
pause
