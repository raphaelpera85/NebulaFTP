@echo off
setlocal enabledelayedexpansion
title Nebula STRM Downloader ^& Feeder

echo ===================================================
echo       Nebula STRM Downloader ^& Feeder
echo ===================================================

cd /d "%~dp0"

REM Verificar se o ambiente virtual existe
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo Usando Python: %PYTHON_EXE%
echo Iniciando monitoramento e download de arquivos .strm...
echo.

"%PYTHON_EXE%" tools\strm_downloader.py --watch %*

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERRO] O processo foi encerrado com codigo de erro %ERRORLEVEL%.
    pause
)
