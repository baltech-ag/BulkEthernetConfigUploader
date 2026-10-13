@echo off
setlocal EnableExtensions
rem BulkEthernetConfigUploader - starts the GUI in the project's Python environment.
rem Needs uv on the PATH (https://docs.astral.sh/uv/). On the first start uv downloads Python 3.13 and the
rem packages into its own folders in the user profile (internet needed); later starts take a few seconds.
rem The console window closes two seconds after the GUI has started.
rem Set BECU_START_CHECK_ONLY=1 to prepare the environment without starting the GUI.

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
set "LOG=%ROOT%\logs\start.log"
echo [%date% %time%] Start.cmd >> "%LOG%"

echo BulkEthernetConfigUploader
echo Program folder: %ROOT%

rem --- the Python launcher fails in very long folder paths (Windows MAX_PATH) ---
set "ROOT_LENGTH=0"
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command "('%ROOT%').Length"`) do set "ROOT_LENGTH=%%L"
if %ROOT_LENGTH% GTR 150 (
    echo The folder path is %ROOT_LENGTH% characters long. Move the program to a shorter path, for example C:\BECU.
    goto :fail
)

rem --- uv must be installed ---
where uv >nul 2>&1
if errorlevel 1 (
    echo uv is not installed or not on the PATH. Install it from https://docs.astral.sh/uv/ and start again.
    goto :fail
)
set "UV_VERSION="
for /f "usebackq delims=" %%V in (`uv --version`) do set "UV_VERSION=%%V"
echo Package manager: %UV_VERSION%
set "UV_NO_PROGRESS=1"

rem --- Python environment: create or repair (.venv in this folder; Python and packages from uv) ---
echo Checking the Python environment ... the first start downloads Python 3.13 and the packages.
call :prepare
if errorlevel 1 (
    echo The environment is broken, recreating it ...
    if exist "%VENV%" rmdir /s /q "%VENV%"
    call :prepare
    if errorlevel 1 (
        echo Preparing the Python environment failed. See %LOG%.
        goto :fail
    )
)
echo Python environment ready.
if defined BECU_START_CHECK_ONLY (
    echo Environment check finished.
    exit /b 0
)

rem --- run the GUI without a console window; errors go to logs\becu.log and to a dialog ---
"%PY%" -c "import wx, becu.gui.main_frame" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo The program cannot start. See %LOG%.
    goto :fail
)
echo Starting the user interface ...
start "" "%PYW%" -m becu.gui %*
echo This window closes in two seconds.
timeout /t 2 /nobreak >nul 2>&1
exit /b 0

:prepare
uv sync --frozen >> "%LOG%" 2>&1
if errorlevel 1 exit /b 1
if not exist "%PY%" exit /b 1
exit /b 0

:fail
pause
exit /b 1
