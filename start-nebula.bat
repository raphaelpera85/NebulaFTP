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
set /p MONITOR_PATH="Digite a pasta ou disco para monitorar (ex: E:\ ou D:\Videos) [Padrao: E:\]: "
if "%MONITOR_PATH%"=="" set MONITOR_PATH=E:\

:: Corrige o escape de barra invertida antes de aspas duplas ao passar para o Python
if "%MONITOR_PATH:~-1%"=="\" set "MONITOR_PATH=%MONITOR_PATH%\"

echo.
echo Starting NebulaFTP server...
start /b python -u main.py

echo Starting FTP drive Z: with rclone (background)...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File .\tools\start_rclone_z.ps1' -WindowStyle Hidden"

echo.
echo Starting monitor and feeder for %MONITOR_PATH%...
python .\tools\feed_ftp.py --source "%MONITOR_PATH%" --direct-mongo --workers 2 --watch --max-active 60 --poll-seconds 60 --delete-source

pause
