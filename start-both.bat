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
echo ==============================================
echo  NEBULA FTP - DUAL INSTANCE LAUNCHER
echo ==============================================
echo.
echo This will start BOTH instances:
echo   1. ORIGINAL NEBULA   - FTP:2121  Control:2130  Stream:2122  DB:ftp
echo   2. MULLETAFLIX       - FTP:2123  Control:2131  Stream:2124  DB:ftp_mulletaflix
echo.
echo Make sure you have configured both .env files:
echo   - .env              (Original)
echo   - .env.mulletaflix  (MulletaFlix)
echo.

set /p CONFIRM="Start both instances? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo [1/2] Starting ORIGINAL NEBULA...
copy /y ".env" ".env.backup" >nul 2>&1
start /b python -u main.py
echo Original Nebula started (PID captured in background).

echo.
echo [2/2] Starting MULLETAFLIX...
copy /y ".env.mulletaflix" ".env" >nul
start /b python -u main.py
echo MulletaFlix started (PID captured in background).

:: Restore original .env
copy /y ".env.backup" ".env" >nul 2>&1
del ".env.backup" >nul 2>&1

echo.
echo ==============================================
echo  BOTH INSTANCES STARTED
echo ==============================================
echo.
echo Original Nebula:   ftp://localhost:2121  |  http://localhost:2130  |  http://localhost:2122
echo MulletaFlix:       ftp://localhost:2123  |  http://localhost:2131  |  http://localhost:2124
echo.
echo MongoDB Databases: Original=ftp  |  MulletaFlix=ftp_mulletaflix
echo.
echo To stop: Close this window or use Task Manager to kill python.exe processes.
echo.
pause