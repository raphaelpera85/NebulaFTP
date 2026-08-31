@echo off
setlocal
cd /d "%~dp0"

:: Ativa o ambiente virtual se existir
if exist "..\.venv\Scripts\activate.bat" (
    call "..\.venv\Scripts\activate.bat"
) else if exist ".\.venv\Scripts\activate.bat" (
    call ".\.venv\Scripts\activate.bat"
)

echo Checking Python dependencies...
python .\tools\bootstrap.py
if errorlevel 1 (
  echo Failed to prepare Python dependencies.
  pause
  exit /b 1
)

echo.
set /p MONITOR_PATHS="Digite uma ou mais pastas para monitorar separadas por ; (ex: E:\;D:\Videos) [Padrao: D:/midias]: "
if "%MONITOR_PATHS%"=="" set MONITOR_PATHS=D:/midias

:: Corrige o escape de barra invertida antes de aspas duplas ao passar para o Python
if "%MONITOR_PATHS:~-1%"=="\" set "MONITOR_PATHS=%MONITOR_PATHS%\"

echo.
echo Starting NebulaFTP server...
start /b python -u main.py

echo Starting FTP drive N: with rclone (background)...
start "" /b powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tools\start_rclone_z.ps1"

echo Starting cleanup bot for already sent media...
start /b python -u .\tools\clean_already_sent.py --sources "%MONITOR_PATHS%" --interval 30

echo.
echo Starting monitor and feeder for %MONITOR_PATHS%...
python .\tools\feed_ftp.py --source "%MONITOR_PATHS%" --direct-mongo --workers 2 --watch --max-active 60 --poll-seconds 60 --delete-source --prune-completed-strm

pause
