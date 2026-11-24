@echo off
rem Starts tret (http://localhost:5180) using Docker. Safe to run repeatedly.
setlocal EnableExtensions
title tret - start

rem Always work from the folder this file lives in, so double-clicking works.
cd /d "%~dp0"

set "FRONTEND_URL=http://localhost:5180"
set "HEALTH_URL=http://localhost:8000/api/healthz"

rem -- 1. Is Docker installed? ------------------------------------------------
where docker >nul 2>nul
if errorlevel 1 goto no_docker

rem -- 2. Is Docker running? If not, try to start it and wait. ---------------
docker info >nul 2>nul
if errorlevel 1 goto start_docker
goto docker_ready

:no_docker
echo.
echo tret runs inside Docker, and Docker doesn't seem to be installed yet.
echo Opening the Docker Desktop download page in your browser now.
echo.
echo Install Docker Desktop, open it once, then double-click this file again.
start "" "https://www.docker.com/products/docker-desktop/"
echo.
pause
exit /b 1

:start_docker
echo.
echo Docker is installed but not running yet.
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" goto launch_pf
if exist "%LocalAppData%\Docker\Docker Desktop.exe" goto launch_la
echo Please open the Docker Desktop app yourself - I could not find it in the
echo usual place. I will wait here while it starts...
goto wait_docker_start

:launch_pf
echo Starting Docker Desktop for you - this can take a minute...
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
goto wait_docker_start

:launch_la
echo Starting Docker Desktop for you - this can take a minute...
start "" "%LocalAppData%\Docker\Docker Desktop.exe"
goto wait_docker_start

:wait_docker_start
set /a WAITED=0
echo|set /p="Waiting for Docker to wake up "

:wait_docker
docker info >nul 2>nul
if not errorlevel 1 goto docker_woke
if %WAITED% GEQ 120 goto docker_timeout
echo|set /p="."
timeout /t 3 /nobreak >nul
set /a WAITED=WAITED+3
goto wait_docker

:docker_timeout
echo.
echo.
echo Docker didn't start within 2 minutes.
echo Open the Docker Desktop app yourself, wait for the whale icon in the
echo taskbar to settle, then double-click this file again.
echo.
pause
exit /b 1

:docker_woke
echo  ready!

:docker_ready
rem -- 3. First run: create .env from the example. ---------------------------
if exist ".env" goto have_env
rem .env.example ships a placeholder secret key and admin password. A new .env
rem must never keep them, so generate random ones (URL-safe, from the OS CSPRNG).
rem First, best effort: is there a database volume left from an earlier install?
rem Compose names it <project>_pgdata (project = folder name, lowercased, with
rem characters outside [a-z0-9_-] and leading _ or - dropped, unless COMPOSE_PROJECT_NAME is set).
set "ENV_EXISTING_DB="
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=$env:COMPOSE_PROJECT_NAME; if(-not $p){$p=(Split-Path -Leaf (Get-Location).Path).ToLower() -replace '[^a-z0-9_-]','' -replace '^[_-]+',''}; if(-not $p){exit 1}; docker volume inspect ($p+'_pgdata') *> $null; exit $LASTEXITCODE" >nul 2>nul
if not errorlevel 1 set "ENV_EXISTING_DB=1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$g={param($n) $b=New-Object byte[] $n; $r=[System.Security.Cryptography.RandomNumberGenerator]::Create(); $r.GetBytes($b); [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','-').Replace('/','_')}; $k=& $g 32; $p=& $g 15; $o=foreach($l in [IO.File]::ReadAllLines('.env.example')){ if($l -like 'TRET_SECRET_KEY=*'){'TRET_SECRET_KEY='+$k} elseif($l -like 'TRET_ADMIN_PASSWORD=*'){'TRET_ADMIN_PASSWORD='+$p} else {$l} }; [IO.File]::WriteAllLines('.env.tmp',$o)"
if errorlevel 1 goto env_failed
if not exist ".env.tmp" goto env_failed
move /y ".env.tmp" ".env" >nul
if errorlevel 1 goto env_failed
set "ENV_IS_NEW=1"
echo.
echo Created a settings file named .env from the example that ships with
echo tret, with a freshly generated secret key and admin password.
echo You don't need to edit it - AI provider keys are added later,
echo inside the app itself on the Settings page, not in this file.
call :warn_existing_db

:have_env
call :read_env
if not defined ENV_IS_NEW call :warn_defaults

rem -- 4. Start everything. --------------------------------------------------
echo.
echo Starting tret. The very first start downloads and builds everything,
echo which can take several minutes - later starts take only seconds.
echo.
docker compose up -d --build
if errorlevel 1 goto compose_failed

rem -- 5. Wait until tret answers, then open the browser. -------------------
echo.
set /a WAITED=0
echo|set /p="Waiting for tret to finish starting up "

:wait_health
curl.exe -f -s -o NUL --max-time 3 "%HEALTH_URL%" 2>nul
if not errorlevel 1 goto healthy
if %WAITED% GEQ 300 goto health_timeout
echo|set /p="."
timeout /t 3 /nobreak >nul
set /a WAITED=WAITED+3
goto wait_health

:health_timeout
echo.
echo.
echo tret didn't come up within 5 minutes. It may still be building - you
echo can simply wait a bit and then open %FRONTEND_URL% yourself,
echo or see what it's doing by running this here:  docker compose logs backend
echo.
pause
exit /b 1

:healthy
echo  it's up!
echo.
echo Opening tret in your browser: %FRONTEND_URL%
start "" "%FRONTEND_URL%"
echo.
echo ------------------------------------------------------------
call :print_login
echo   tret is reachable from this computer only.
echo.
echo   Add your AI provider key inside the app: Settings page.
echo   To stop tret later, double-click stop-tret.bat.
echo ------------------------------------------------------------
call :warn_existing_db
call :warn_defaults
echo.
pause
exit /b 0

:env_failed
if exist ".env.tmp" del ".env.tmp" >nul 2>nul
echo.
echo Could not create the .env settings file with generated credentials.
echo PowerShell is needed for this, and locked-down Windows (PowerShell
echo Constrained Language Mode, or policies that block PowerShell) can stop it.
echo To continue, copy .env.example to .env by hand, then edit .env and replace
echo the values of TRET_SECRET_KEY and TRET_ADMIN_PASSWORD with two long random
echo strings (a password manager's generator works). Then double-click this
echo file again.
echo.
pause
exit /b 1


:read_env
rem Last assignment wins, like docker compose. Read with delayed expansion OFF
rem (so ! survives); %%~B drops one pair of surrounding quotes. Unset/empty
rem values stay empty: the callers treat them as docker-compose.yml's defaults.
set "ADMIN_EMAIL="
set "ADMIN_PW="
set "SECRET_KEY="
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /c:"TRET_ADMIN_EMAIL=" ".env"`) do set "ADMIN_EMAIL=%%~B"
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /c:"TRET_ADMIN_PASSWORD=" ".env"`) do set "ADMIN_PW=%%~B"
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /c:"TRET_SECRET_KEY=" ".env"`) do set "SECRET_KEY=%%~B"
exit /b 0

:print_login
rem Delayed expansion here so quotes and & < > | in values cannot break parsing.
setlocal EnableDelayedExpansion
set "SHOW_EMAIL=!ADMIN_EMAIL!"
if "!SHOW_EMAIL!"=="" set "SHOW_EMAIL=admin@example.com"
set "SHOW_PW=!ADMIN_PW!"
if "!SHOW_PW!"=="" set "SHOW_PW=tret-admin"
echo(  Log in with:   !SHOW_EMAIL!
if defined ENV_EXISTING_DB goto print_login_old
echo(  Password:      !SHOW_PW!
echo   (kept as TRET_ADMIN_PASSWORD in the .env file in this folder; it is
echo   used when the admin account is first created - change it later in
echo   the app.)
endlocal
exit /b 0
:print_login_old
echo   Password:      unchanged - your existing database keeps its admin
echo   password (tret-admin if you never changed it). The new one in .env
echo   is not used.
endlocal
exit /b 0

:warn_existing_db
if not defined ENV_EXISTING_DB exit /b 0
echo.
echo !! An existing tret database was found from an earlier install.
echo    It keeps its existing admin password (tret-admin if never changed),
echo    not the new one in .env. AI provider keys saved earlier were encrypted
echo    with the old secret key and must be re-entered in the app.
exit /b 0

:warn_defaults
setlocal EnableDelayedExpansion
set "SKW="
set "PWW="
if "!SECRET_KEY!"=="" set "SKW=1"
if "!SECRET_KEY!"=="dev-secret-change-me" set "SKW=1"
if "!ADMIN_PW!"=="" set "PWW=1"
if "!ADMIN_PW!"=="tret-admin" set "PWW=1"
set "WEAK="
if defined SKW set "WEAK=secret key"
if defined PWW set "WEAK=admin password"
if defined SKW if defined PWW set "WEAK=secret key and admin password"
if not defined WEAK endlocal & exit /b 0
set "WEAK_TXT=!WEAK!"
echo.
echo WARNING: the .env file here still has the published default (or unset) !WEAK_TXT!.
echo    Anyone who can reach this computer can sign in as admin. Fix it:
echo      1. In .env set TRET_SECRET_KEY and TRET_ADMIN_PASSWORD to new
echo         random values. (Changing the secret key makes AI provider keys
echo         saved in the app unreadable - re-enter them afterwards.)
echo      2. The admin account exists already, so also change its password
echo         in the app, then stop and start tret again.
endlocal
exit /b 0

:compose_failed
echo.
echo Something went wrong while starting tret - the messages above have the
echo details. If you're stuck, run this here to see more:
echo   docker compose logs backend
echo.
pause
exit /b 1
