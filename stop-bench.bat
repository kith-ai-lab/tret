@echo off
rem Stops bench. Your data is kept - double-click start-bench.bat to come back.
setlocal EnableExtensions
title bench - stop
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 goto already_stopped
docker info >nul 2>nul
if errorlevel 1 goto already_stopped

echo.
echo Stopping bench...
docker compose down
if errorlevel 1 goto stop_failed

echo.
echo bench is stopped. All your data is kept safe - just double-click
echo start-bench.bat whenever you want to come back.
echo.
pause
exit /b 0

:already_stopped
echo.
echo Docker isn't running, so bench is already stopped. Nothing to do.
echo.
pause
exit /b 0

:stop_failed
echo.
echo Something went wrong while stopping. You can try again, or run
echo "docker compose down" in this folder.
echo.
pause
exit /b 1
