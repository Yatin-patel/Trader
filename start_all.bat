@echo off
REM ============================================================================
REM  Autonomous Trader — one-shot bootstrap + run
REM    1. Creates a virtualenv if missing
REM    2. Installs / upgrades requirements
REM    3. Copies .env.example -> .env if .env is missing
REM    4. Generates a SECRET_ENCRYPTION_KEY if blank
REM    5. Initializes SQL Server Express database + schema
REM    6. Launches the API + autonomous runner
REM ============================================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo [trader] === Autonomous Wheel Trader bootstrap ===
echo [trader] working dir: %CD%
echo.

REM ---------- 1. Locate Python ------------------------------------------------
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PYLAUNCH=py -3"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL%==0 (
        set "PYLAUNCH=python"
    ) else (
        echo [trader] ERROR: Python 3.12+ not found on PATH. Install from https://www.python.org/
        exit /b 1
    )
)

REM ---------- 2. Create virtualenv -------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [trader] Creating virtual environment in .venv ...
    %PYLAUNCH% -m venv .venv
    if errorlevel 1 (
        echo [trader] ERROR: failed to create venv
        exit /b 1
    )
) else (
    echo [trader] Virtual environment already present.
)

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PIP=.venv\Scripts\pip.exe"

REM ---------- 3. Install requirements ----------------------------------------
echo [trader] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip --disable-pip-version-check >nul

echo [trader] Installing requirements (this may take a minute the first time) ...
"%VENV_PIP%" install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo [trader] ERROR: pip install failed.
    exit /b 1
)

REM ---------- 4. Ensure .env exists ------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        echo [trader] No .env found - copying from .env.example
        copy /Y ".env.example" ".env" >nul
    ) else (
        echo [trader] WARNING: .env.example missing - continuing with defaults
    )
)

REM ---------- 5. Generate SECRET_ENCRYPTION_KEY if blank ---------------------
findstr /B /C:"SECRET_ENCRYPTION_KEY=" .env >nul 2>&1
if %ERRORLEVEL%==0 (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /B /C:"SECRET_ENCRYPTION_KEY=" .env`) do (
        set "CUR_KEY=%%B"
    )
    if "!CUR_KEY!"=="" (
        echo [trader] Generating SECRET_ENCRYPTION_KEY ...
        for /f "delims=" %%K in ('"%VENV_PY%" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"') do (
            set "NEW_KEY=%%K"
        )
        powershell -NoProfile -Command "(Get-Content .env) -replace '^SECRET_ENCRYPTION_KEY=.*', 'SECRET_ENCRYPTION_KEY=!NEW_KEY!' | Set-Content -Encoding ASCII .env"
        echo [trader] SECRET_ENCRYPTION_KEY written to .env
    ) else (
        echo [trader] SECRET_ENCRYPTION_KEY already set.
    )
) else (
    echo [trader] Appending SECRET_ENCRYPTION_KEY to .env ...
    for /f "delims=" %%K in ('"%VENV_PY%" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"') do (
        echo SECRET_ENCRYPTION_KEY=%%K>> .env
    )
)

REM ---------- 6. Bootstrap database ------------------------------------------
echo [trader] Initializing SQL Server Express database + schema ...
"%VENV_PY%" main.py initdb
if errorlevel 1 (
    echo [trader] ERROR: database bootstrap failed.
    echo [trader]   Verify SQL Server Express is running and ODBC Driver 17/18 is installed.
    echo [trader]   Check DB_SERVER / DB_NAME / DB_TRUSTED_CONNECTION in .env
    exit /b 1
)

REM ---------- 7. Launch app --------------------------------------------------
echo.
echo [trader] ============================================================
echo [trader]  Starting API + autonomous runner
echo [trader]  Dashboard:        http://127.0.0.1:8000/dashboard
echo [trader]  Global settings:  http://127.0.0.1:8000/settings
echo [trader]  Press Ctrl+C to stop.
echo [trader] ============================================================
echo.

"%VENV_PY%" main.py all
set "EXITCODE=%ERRORLEVEL%"

echo.
echo [trader] Application exited with code %EXITCODE%
endlocal & exit /b %EXITCODE%
