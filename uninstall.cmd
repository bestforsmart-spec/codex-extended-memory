@echo off
setlocal
rem Extended memory for Codex: uninstaller for Windows.
rem Usage: uninstall.cmd [--purge]
rem Your notes and MEMORY.md stay in place unless --purge is given.

set "HERE=%~dp0"
if exist "%HERE%app\setup_windows.py" goto :find_python
echo.
echo   Nothing to uninstall here: there is no "app" folder next to uninstall.cmd.
echo.
pause
exit /b 1

:find_python
set "PYCMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :run
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto :run
echo.
echo   Python 3.8 or newer is required, and it was not found.
echo   Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo   then open a new terminal window and run uninstall.cmd again.
echo.
pause
exit /b 1

:run
%PYCMD% "%HERE%app\setup_windows.py" uninstall %* & exit /b
