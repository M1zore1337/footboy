@echo off
setlocal
if defined FOOTBOY_PYTHON goto configured
"%~dp0.venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto virtualenv
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto launcher
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python
echo Footboy requires Python 3.10+. Install it or set FOOTBOY_PYTHON to its executable. 1>&2
exit /b 1

:configured
"%FOOTBOY_PYTHON%" "%~dp0scripts\bootstrap.py" start %*
exit /b %errorlevel%

:virtualenv
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\bootstrap.py" start %*
exit /b %errorlevel%

:launcher
py -3 "%~dp0scripts\bootstrap.py" start %*
exit /b %errorlevel%

:python
python "%~dp0scripts\bootstrap.py" start %*
exit /b %errorlevel%
