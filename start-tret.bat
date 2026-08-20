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
copy /y ".env.example" ".env" >nul
echo.
echo Created a settings file named .env from the example that ships with
echo tret. You don't need to edit it - AI provider keys are added later,
echo inside the app itself on the Settings page, not in this file.

:have_env
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
echo   Log in with:   admin@example.com  /  tret-admin
echo   Change these in the .env file if anyone else can
echo   reach this computer.
echo.
echo   Add your AI provider key inside the app: Settings page.
echo   To stop tret later, double-click stop-tret.bat.
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:compose_failed
echo.
echo Something went wrong while starting tret - the messages above have the
echo details. If you're stuck, run this here to see more:
echo   docker compose logs backend
echo.
pause
exit /b 1
