param (
    [string]$Source = "E:\"
)
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Set-Location (Split-Path -Parent $PSScriptRoot)

python .\tools\bootstrap.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python .\tools\feed_ftp.py --source $Source --direct-mongo --workers 2 --watch --max-active 60 --poll-seconds 60 --delete-source 2>&1 | Tee-Object -FilePath ".\feed_ftp.log" -Append
