$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Set-Location (Split-Path -Parent $PSScriptRoot)

function Find-Rclone {
  $cmd = Get-Command rclone.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter rclone.exe -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  if (-not $found) {
    if (Test-Path ".\tools\rclone.exe") { return (Resolve-Path ".\tools\rclone.exe").Path }
  }
  return $found
}

function Test-WinFsp {
  $apps = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue
  return [bool]($apps | Where-Object { $_.DisplayName -like "WinFsp*" } | Select-Object -First 1)
}

function Install-WingetPackage($id) {
  if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw "winget nao encontrado. Instale rclone/WinFsp manualmente ou habilite o App Installer."
  }
  winget install --id $id --silent --accept-package-agreements --accept-source-agreements
}

if (-not (Test-WinFsp)) {
  Write-Host "Instalando WinFsp..."
  Install-WingetPackage "WinFsp.WinFsp"
}

$rclone = Find-Rclone
if (-not $rclone) {
  Write-Host "Instalando rclone..."
  Install-WingetPackage "Rclone.Rclone"
  $rclone = Find-Rclone
}
if (-not $rclone) { throw "rclone nao encontrado apos instalacao." }

$targetDrive = "N"
if (Get-PSDrive $targetDrive -ErrorAction SilentlyContinue) {
  $rcloneMountN = Get-CimInstance Win32_Process -Filter "name = 'rclone.exe'" |
    Where-Object { $_.CommandLine -like "*mount nebula:*" -and $_.CommandLine -like "*N:*" } |
    Select-Object -First 1
  if ($rcloneMountN) {
    Write-Host "N: ja esta montado pelo rclone."
    exit 0
  }
  throw "N: ja esta em uso por outro programa. Desmonte o mapeamento existente antes de iniciar."
}

$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
  try {
    $client = [Net.Sockets.TcpClient]::new()
    $client.Connect("127.0.0.1", 2121)
    $client.Close()
    break
  } catch {
    Start-Sleep -Seconds 2
  }
}

$configFile = (Resolve-Path ".\rclone-nebula.conf").Path
$rcloneLog = Join-Path ([System.IO.Path]::GetTempPath()) "rclone-mount.log"
& $rclone mount nebula:/ "$($targetDrive):" `
  --config "$configFile" `
  --vfs-cache-mode full `
  --vfs-cache-max-size 20G `
  --dir-cache-time 10s `
  --poll-interval 0 `
  --links `
  --no-checksum `
  --network-mode `
  --log-file "$rcloneLog" `
  --log-level INFO
