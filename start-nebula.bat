@echo off
setlocal
cd /d "%~dp0"

echo Checking Python dependencies...
python .\tools\bootstrap.py
if errorlevel 1 (
  echo Failed to prepare Python dependencies.
  pause
  exit /b 1
)

echo.
set /p MONITOR_PATHS="Digite uma ou mais pastas para monitorar separadas por ; (ex: E:\;D:\Videos) [Padrao: E:\]: "
if "%MONITOR_PATHS%"=="" set MONITOR_PATHS=E:\

:: Corrige o escape de barra invertida antes de aspas duplas ao passar para o Python
if "%MONITOR_PATHS:~-1%"=="\" set "MONITOR_PATHS=%MONITOR_PATHS%\"

echo.
echo Starting NebulaFTP server...
start /b python -u main.py

echo Starting FTP drive Z: with rclone (background)...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File .\tools\start_rclone_z.ps1' -WindowStyle Hidden"

echo.
echo Starting monitor and feeder for %MONITOR_PATHS%...
python .\tools\feed_ftp.py --source "%MONITOR_PATHS%" --direct-mongo --workers 2 --watch --max-active 60 --poll-seconds 60 --delete-source

pause
