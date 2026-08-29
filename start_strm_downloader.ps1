# PowerShell Runner para Nebula STRM Downloader & Feeder
$Host.UI.RawUI.WindowTitle = "Nebula STRM Downloader & Feeder"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$PythonExe = "python"
if (Test-Path "$ProjectRoot\.venv\Scripts\python.exe") {
    $PythonExe = "$ProjectRoot\.venv\Scripts\python.exe"
}

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "      Nebula STRM Downloader & Feeder             " -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "Interpretador Python: $PythonExe" -ForegroundColor Yellow
Write-Host "Iniciando processo..." -ForegroundColor Green
Write-Host ""

& $PythonExe "$ProjectRoot\tools\strm_downloader.py" --watch @args
