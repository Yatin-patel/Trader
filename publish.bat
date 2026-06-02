@echo off
REM ============================================================================
REM  Autonomous Trader - publish.bat
REM
REM  One-shot installer for a fresh Windows server. Each step:
REM    * Detects whether the component is already present.
REM    * Skips it cleanly if so.
REM    * Installs or configures it if not.
REM
REM  The script is idempotent: re-running it on an already-configured server
REM  walks through every step and reports "skipped" for anything in place.
REM
REM  Steps:
REM    1.  Locate Python (>= 3.10)
REM    2.  Verify SQL Server ODBC driver (17 or 18) is installed
REM    3.  Verify SQL Server / SQLEXPRESS is reachable
REM    4.  Verify Microsoft Visual C++ Build Tools (needed for argon2 etc.)
REM    5.  Create the .venv if missing
REM    6.  pip install -r requirements.txt (pip itself skips installed packages
REM        when they match the pinned version)
REM    7.  Copy .env.example -> .env if .env missing
REM    8.  Generate SECRET_ENCRYPTION_KEY if blank
REM    9.  Create the trader_backups directory used by daily backup job
REM   10.  Initialise the database schema (idempotent — uses IF NOT EXISTS)
REM   11.  Add a LAN-scoped Windows Firewall rule for port 8000
REM   12.  Patch .env so API_HOST binds 0.0.0.0 (lets nginx reach the app)
REM   13.  Detect the Windows app server's LAN IP for the nginx upstream
REM   14.  Generate deploy\nginx_traderapp.conf with that IP substituted in
REM   15.  Print scp + bash commands for the nginx server (192.168.1.102),
REM        which runs deploy\install_on_nginx_server.sh to set up the site,
REM        get a Let's Encrypt cert, and enable cert auto-renewal
REM
REM  Exit codes:
REM    0  -- all steps completed (some may have been skipped, that is fine)
REM    1  -- a required step failed and the install cannot continue
REM
REM  After this script returns 0, launch the app with start_all.bat.
REM ============================================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "STEP_OK= [ok]    "
set "STEP_SKP=[skip]  "
set "STEP_DO= [do]    "
set "STEP_ERR=[error] "

echo.
echo ============================================================
echo   Autonomous Trader - publish
echo   Working dir: %CD%
echo ============================================================
echo.

REM ---------- 1. Locate Python ------------------------------------------------
echo [1/15] Python 3.10+
where py >nul 2>&1
if not errorlevel 1 (
    set "PYLAUNCH=py -3"
    for /f "delims=" %%v in ('py -3 --version 2^>^&1') do set "PYV=%%v"
    echo %STEP_OK% Found via py launcher: !PYV!
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PYLAUNCH=python"
        for /f "delims=" %%v in ('python --version 2^>^&1') do set "PYV=%%v"
        echo %STEP_OK% Found via python: !PYV!
    ) else (
        echo %STEP_ERR% Python NOT FOUND on PATH.
        echo            Install from: https://www.python.org/downloads/
        echo            Tick "Add Python to PATH" during install, then re-run publish.bat.
        goto :failed
    )
)

REM ---------- 2. SQL Server ODBC driver ---------------------------------------
echo [2/15] SQL Server ODBC driver (17 or 18)
set "DRIVER_FOUND="
reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server" >nul 2>&1
if not errorlevel 1 set "DRIVER_FOUND=17"
reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server" >nul 2>&1
if not errorlevel 1 set "DRIVER_FOUND=18"
if defined DRIVER_FOUND (
    echo %STEP_OK% Driver !DRIVER_FOUND! present.
) else (
    echo %STEP_ERR% No ODBC Driver 17 or 18 for SQL Server installed.
    echo            Download: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
    echo            After install, re-run publish.bat.
    goto :failed
)

REM ---------- 3. SQL Server / SQLEXPRESS reachable ----------------------------
echo [3/15] SQL Server reachable
sc query "MSSQL$SQLEXPRESS" >nul 2>&1
if not errorlevel 1 (
    echo %STEP_OK% Service MSSQL$SQLEXPRESS detected.
) else (
    sc query "MSSQLSERVER" >nul 2>&1
    if not errorlevel 1 (
        echo %STEP_OK% Service MSSQLSERVER detected.
    ) else (
        echo %STEP_ERR% No SQL Server service detected.
        echo            Install SQL Server 2019/2022 Express:
        echo              https://www.microsoft.com/en-us/sql-server/sql-server-downloads
        echo            Pick "Basic" install. Default instance name SQLEXPRESS is fine.
        echo            After install, re-run publish.bat.
        goto :failed
    )
)

REM ---------- 4. Visual C++ Build Tools ---------------------------------------
REM Some pinned packages (argon2-cffi, cryptography on certain Python versions)
REM require a C compiler. The %ProgramFiles(x86)% env var contains a literal
REM ")" which breaks `if (...)` blocks, so we do flat sequential checks here.
echo [4/15] Microsoft Visual C++ Build Tools
set "VC_FOUND="
where cl >nul 2>&1
if not errorlevel 1 set "VC_FOUND=PATH"
if not defined VC_FOUND if exist "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC" set "VC_FOUND=BuildTools 2022"
call set "VC_X86=%%ProgramFiles(x86)%%"
if not defined VC_FOUND if exist "!VC_X86!\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC" set "VC_FOUND=BuildTools 2022 x86"
if defined VC_FOUND (
    echo %STEP_OK% Visual C++ Build Tools detected ^(!VC_FOUND!^).
) else (
    echo %STEP_SKP% Visual C++ Build Tools not detected.
    echo            Most wheels for our deps are pre-built; this is usually fine.
    echo            If pip install fails with "Microsoft Visual C++ 14.0 or greater is required",
    echo            install from: https://visualstudio.microsoft.com/visual-cpp-build-tools/
)

REM ---------- 5. Virtual environment ------------------------------------------
echo [5/15] Python virtual environment (.venv)
if exist ".venv\Scripts\python.exe" (
    echo %STEP_SKP% .venv already exists.
) else (
    echo %STEP_DO% Creating .venv ...
    %PYLAUNCH% -m venv .venv
    if errorlevel 1 (
        echo %STEP_ERR% Failed to create .venv.
        goto :failed
    )
    echo %STEP_OK% Created.
)
set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PIP=.venv\Scripts\pip.exe"

REM ---------- 6. pip install requirements -------------------------------------
REM pip install -r requirements.txt is naturally idempotent: it leaves
REM already-installed packages of the pinned version untouched and only
REM downloads what is missing.
echo [6/15] Python packages from requirements.txt
"%VENV_PY%" -m pip install --upgrade pip --disable-pip-version-check >nul 2>&1
"%VENV_PIP%" install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo %STEP_ERR% pip install failed. See output above.
    echo            If it complained about Visual C++ Build Tools, install them
    echo            from https://visualstudio.microsoft.com/visual-cpp-build-tools/
    echo            then re-run publish.bat.
    goto :failed
)
echo %STEP_OK% Python packages up to date.

REM ---------- 7. .env file ----------------------------------------------------
echo [7/15] .env configuration file
if exist ".env" (
    echo %STEP_SKP% .env already present.
) else (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        echo %STEP_OK% Created .env from .env.example.
        echo            Review settings before first run, especially DB_SERVER if
        echo            your SQL Server instance is not localhost\SQLEXPRESS.
    ) else (
        echo %STEP_ERR% Neither .env nor .env.example exists.
        echo            Cannot continue without a configuration template.
        goto :failed
    )
)

REM ---------- 8. SECRET_ENCRYPTION_KEY ----------------------------------------
REM We generate via a Python heredoc-style call. Inner double-quotes break the
REM `for /f (' ... ')` form on Windows batch, so we route through a temp file.
echo [8/15] SECRET_ENCRYPTION_KEY (Fernet, encrypts broker keys at rest)
set "KEY_TMP=%TEMP%\trader_fernet_%RANDOM%.tmp"

findstr /B /C:"SECRET_ENCRYPTION_KEY=" .env >nul 2>&1
if not errorlevel 1 (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /B /C:"SECRET_ENCRYPTION_KEY=" .env`) do set "CUR_KEY=%%B"
    if "!CUR_KEY!"=="" (
        echo %STEP_DO% Generating new key ...
        "%VENV_PY%" -c "from cryptography.fernet import Fernet; open(r'%KEY_TMP%','w').write(Fernet.generate_key().decode())"
        if errorlevel 1 (
            echo %STEP_ERR% Failed to generate key via Python.
            goto :failed
        )
        set /p NEW_KEY=<"%KEY_TMP%"
        del "%KEY_TMP%" 2>nul
        powershell -NoProfile -Command "(Get-Content .env) -replace '^SECRET_ENCRYPTION_KEY=.*', 'SECRET_ENCRYPTION_KEY=!NEW_KEY!' | Set-Content -Encoding ASCII .env"
        echo %STEP_OK% Key written to .env.
    ) else (
        echo %STEP_SKP% Key already set.
    )
) else (
    echo %STEP_DO% Appending key to .env ...
    "%VENV_PY%" -c "from cryptography.fernet import Fernet; open(r'%KEY_TMP%','w').write(Fernet.generate_key().decode())"
    if errorlevel 1 (
        echo %STEP_ERR% Failed to generate key via Python.
        goto :failed
    )
    set /p NEW_KEY=<"%KEY_TMP%"
    del "%KEY_TMP%" 2>nul
    echo SECRET_ENCRYPTION_KEY=!NEW_KEY!>> .env
    echo %STEP_OK% Key appended.
)

REM ---------- 9. trader_backups directory -------------------------------------
echo [9/15] Backup directory C:\trader_backups
if exist "C:\trader_backups" (
    echo %STEP_SKP% Already exists.
) else (
    mkdir "C:\trader_backups" 2>nul
    if errorlevel 1 (
        echo %STEP_SKP% Could not create C:\trader_backups ^(permission?^).
        echo            Daily backup will fail silently until the dir exists.
        echo            Create it manually or edit backup_dir in Global Settings.
    ) else (
        echo %STEP_OK% Created C:\trader_backups.
    )
)

REM ---------- 10. Database schema --------------------------------------------
echo [10/15] Database + schema (CREATE DATABASE + apply schema.sql)
"%VENV_PY%" main.py initdb
if errorlevel 1 (
    echo %STEP_ERR% Database bootstrap failed.
    echo            Verify SQL Server is running and your .env points at it.
    echo            DB_SERVER ^/ DB_NAME ^/ DB_TRUSTED_CONNECTION must be correct.
    goto :failed
)
echo %STEP_OK% Schema applied ^(every CREATE uses IF NOT EXISTS, so re-running this
echo            step on an existing DB is safe^).

REM ---------- 11. Windows Firewall rule for port 8000 (LAN only) -------------
REM Scoped to LocalSubnet so the nginx box on 192.168.1.0/24 can reach us
REM but the port stays closed to the public internet.
echo [11/15] Windows Firewall rule for port 8000 (LocalSubnet)
netsh advfirewall firewall show rule name="AutonomousTrader-8000" >nul 2>&1
if not errorlevel 1 (
    echo %STEP_SKP% Rule "AutonomousTrader-8000" already present.
) else (
    netsh advfirewall firewall add rule name="AutonomousTrader-8000" dir=in action=allow protocol=TCP localport=8000 remoteip=LocalSubnet >nul 2>&1
    if errorlevel 1 (
        echo %STEP_SKP% Could not add firewall rule ^(run as Administrator to add^).
        echo            The app still runs on localhost without this rule, but
        echo            the nginx proxy at 192.168.1.102 will get a connection
        echo            refused until the rule exists.
    ) else (
        echo %STEP_OK% Added inbound rule for TCP/8000 from LocalSubnet.
    )
)

REM ---------- 12. API_HOST=0.0.0.0 so nginx can reach us ---------------------
REM FastAPI binds to API_HOST from .env. If left at 127.0.0.1, the nginx box
REM can't connect. We patch it to 0.0.0.0 only if it's still the localhost
REM default; any other value the user typed manually is left alone.
echo [12/15] Bind app on all interfaces (for nginx proxy)
findstr /B /C:"API_HOST=127.0.0.1" .env >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -Command "(Get-Content .env) -replace '^API_HOST=127\.0\.0\.1', 'API_HOST=0.0.0.0' | Set-Content -Encoding ASCII .env"
    echo %STEP_OK% Patched .env: API_HOST=0.0.0.0
) else (
    findstr /B /C:"API_HOST=0.0.0.0" .env >nul 2>&1
    if not errorlevel 1 (
        echo %STEP_SKP% API_HOST=0.0.0.0 already set.
    ) else (
        echo %STEP_SKP% API_HOST has a custom value; leaving it alone.
    )
)

REM ---------- 13. Detect Windows server's LAN IP -----------------------------
REM The nginx upstream needs an IP, not a hostname. We probe for an IPv4 on
REM 192.168.1.x (the user's stated LAN). Falls back to any private IPv4 if no
REM 192.168.1.x is bound.
echo [13/15] Detect Windows app server's LAN IP
set "APP_IP="
set "IP_TMP=%TEMP%\trader_ip_%RANDOM%.tmp"
powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.1.*' -and $_.IPAddress -ne '192.168.1.102' } | Select-Object -First 1).IPAddress" > "%IP_TMP%" 2>nul
set /p APP_IP=<"%IP_TMP%"
if "!APP_IP!"=="" (
    powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp,Manual | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress" > "%IP_TMP%" 2>nul
    set /p APP_IP=<"%IP_TMP%"
)
del "%IP_TMP%" 2>nul
if "!APP_IP!"=="" (
    echo %STEP_SKP% Could not auto-detect LAN IP. Set APP_IP manually before
    echo            scp'ing deploy/ — edit deploy\nginx_traderapp.conf and
    echo            replace @APP_SERVER_IP@ with your real IP.
    set "APP_IP=@APP_SERVER_IP@"
) else (
    echo %STEP_OK% Detected app server IP: !APP_IP!
)

REM ---------- 14. Generate deploy/ bundle for the nginx server --------------
echo [14/15] Generate deploy\ bundle for the nginx server
if not exist "deploy\nginx_traderapp.conf.template" (
    echo %STEP_ERR% Missing deploy\nginx_traderapp.conf.template — re-check the
    echo            project files were copied correctly.
    goto :failed
)
REM Substitute @APP_SERVER_IP@ in the template -> deploy\nginx_traderapp.conf
powershell -NoProfile -Command "(Get-Content deploy\nginx_traderapp.conf.template) -replace '@APP_SERVER_IP@', '!APP_IP!' | Set-Content -Encoding ASCII deploy\nginx_traderapp.conf"
if errorlevel 1 (
    echo %STEP_ERR% Failed to write deploy\nginx_traderapp.conf.
    goto :failed
)
echo %STEP_OK% Wrote deploy\nginx_traderapp.conf with upstream !APP_IP!:8000

REM ---------- 15. Push deploy\ to nginx server OR print manual cmds ----------
echo [15/15] Deliver deploy\ to nginx server (192.168.1.102)
set "NGINX_HOST=192.168.1.102"
where scp >nul 2>&1
if errorlevel 1 (
    echo %STEP_SKP% scp not found on this Windows host. Install OpenSSH client:
    echo              Settings ^> Apps ^> Optional features ^> "OpenSSH Client"
    echo            Or copy the deploy folder by other means.
    goto :nginx_manual_instructions
)
REM Quick reachability test (don't actually push without a destination user).
ping -n 1 -w 1000 %NGINX_HOST% >nul 2>&1
if errorlevel 1 (
    echo %STEP_SKP% %NGINX_HOST% not pingable from this machine. Push manually:
    goto :nginx_manual_instructions
)

REM We have scp + the host is up, but we don't know the user / can't run
REM `sudo` non-interactively. So we just print copy-paste-ready commands.
echo %STEP_OK% Generated deploy bundle. Copy + run on the nginx server:
:nginx_manual_instructions
echo.
echo   --- On THIS Windows machine ---
echo   scp -r deploy YOUR-USERNAME@%NGINX_HOST%:/tmp/trader-deploy
echo.
echo   --- Then on the nginx server (%NGINX_HOST%) ---
echo   ssh YOUR-USERNAME@%NGINX_HOST%
echo   cd /tmp/trader-deploy
echo   chmod +x install_on_nginx_server.sh
echo   sudo EMAIL=you@example.com ./install_on_nginx_server.sh
echo.
echo   The bash installer is idempotent: rerun it any time to reload nginx
echo   after editing the conf, certbot auto-renewal is enabled at the end.

echo.
echo ============================================================
echo   Publish completed successfully.
echo.
echo   Next steps:
echo     1. Edit .env if any settings need adjusting (DB host, etc.)
echo     2. Run start_all.bat to launch the API + autonomous runner
echo     3. Scp the deploy\ folder to the nginx server and run
echo        install_on_nginx_server.sh (commands printed above)
echo     4. Open https://traderapp.dyndns.org/ in your browser
echo     5. Sign up — the first account becomes admin and inherits
echo        any pre-existing projects.
echo ============================================================
echo.
endlocal & exit /b 0


:failed
echo.
echo ============================================================
echo   Publish FAILED.
echo   Fix the error above and re-run publish.bat — every step
echo   that already succeeded will be skipped.
echo ============================================================
echo.
endlocal & exit /b 1
