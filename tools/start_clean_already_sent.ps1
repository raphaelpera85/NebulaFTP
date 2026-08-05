$scriptPath = Join-Path $PSScriptRoot "clean_already_sent.py"
$pythonPath = "python"

Write-Host "Iniciando Bot de Limpeza Continuo..." -ForegroundColor Green
& $pythonPath $scriptPath --sources "D:/midias" "N:/Filmes" "N:/Series" --interval 30
