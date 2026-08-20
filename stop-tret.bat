@echo off
rem Stops tret. Your data is kept - double-click start-tret.bat to come back.
setlocal EnableExtensions
title tret - stop
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 goto already_stopped
docker info >nul 2>nul
if errorlevel 1 goto already_stopped

echo.
echo Stopping tret...
docker compose down
if errorlevel 1 goto stop_failed

echo.
echo tret is stopped. All your data is kept safe - just double-click
echo start-tret.bat whenever you want to come back.
echo.
pause
exit /b 0

:already_stopped
echo.
echo Docker isn't running, so tret is already stopped. Nothing to do.
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
