@echo off
setlocal
cd /d "%~dp0"

:: Ativa o ambiente virtual se existir
if exist "..\.venv\Scripts\activate.bat" (
    call "..\.venv\Scripts\activate.bat"
) else if exist ".\.venv\Scripts\activate.bat" (
    call ".\.venv\Scripts\activate.bat"
)

echo Verificando dependencias do NebulaFTP...
python .\tools\bootstrap.py
if errorlevel 1 (
  echo Falha ao preparar dependencias do Python.
  pause
  exit /b 1
)

echo.
echo Carregando configuracao Original (.env)...
if exist ".env" (
    echo Configuracao Original ja presente.
) else if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo ATENCAO: .env nao encontrado. Copiado de .env.example - EDITE COM SEUS VALORES REAIS!
    pause
) else (
    echo ERRO: Nem .env nem .env.example encontrados!
    pause
    exit /b 1
)

echo.
set /p MONITOR_PATHS="Digite uma ou mais pastas para monitorar separadas por ; (ex: E:\;D:\Videos) [Padrao: D:/midias]: "
if "%MONITOR_PATHS%"=="" set MONITOR_PATHS=D:/midias

:: Corrige o escape de barra invertida antes de aspas duplas ao passar para o Python
if "%MONITOR_PATHS:~-1%"=="\" set "MONITOR_PATHS=%MONITOR_PATHS%\"

echo.
echo Starting NebulaFTP server (Original on ports 2121/2130/2122)...
start /b python -u main.py

echo Starting FTP drive Z: with rclone (background)...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File .\tools\start_rclone_z.ps1' -WindowStyle Hidden"

echo Starting cleanup bot for already sent media...
start /b python -u .\tools\clean_already_sent.py --sources "%MONITOR_PATHS%" --interval 30

echo.
echo Starting monitor and feeder for %MONITOR_PATHS% (Original)...
python .\tools\feed_ftp.py --source "%MONITOR_PATHS%" --direct-mongo --workers 2 --watch --max-active 60 --poll-seconds 60 --delete-source --prune-completed-strm

pause