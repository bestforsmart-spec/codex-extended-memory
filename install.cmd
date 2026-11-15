@echo off
setlocal
rem Extended memory for Codex: installer for Windows.
rem Usage: install.cmd [options]      List of options: install.cmd --help
rem Safe to run again: updates the program, never touches your memory data.

set "PKG=%~dp0"
if exist "%PKG%app\setup_windows.py" goto :find_python
echo.
echo   The archive is not fully extracted: there is no "app" folder next to install.cmd.
echo   Extract the whole archive first (right click, Extract All), then run install.cmd again.
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
echo   then open a new terminal window and run install.cmd again.
echo.
pause
exit /b 1

:run
%PYCMD% "%PKG%app\setup_windows.py" install %*
exit /b %ERRORLEVEL%
