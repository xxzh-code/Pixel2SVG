@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY_CMD="
where pyw >nul 2>nul && set "PY_CMD=pyw -3"
if not defined PY_CMD where pythonw >nul 2>nul && set "PY_CMD=pythonw"

if defined PY_CMD (
    start "" %PY_CMD% "Pixel2SVG.pyw" %*
    exit /b 0
)

where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD where python >nul 2>nul && set "PY_CMD=python"

if not defined PY_CMD (
    echo Python 3 was not found. Please install Python 3.10+ and try again.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

%PY_CMD% "Pixel2SVG.pyw" %*
if errorlevel 1 pause
