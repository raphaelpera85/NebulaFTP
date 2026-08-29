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

start "" pythonw.exe gui.pyw

