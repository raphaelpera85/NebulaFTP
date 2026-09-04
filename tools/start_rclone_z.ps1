$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Set-Location (Split-Path -Parent $PSScriptRoot)

function Find-Rclone {
  $cmd = Get-Command rclone.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  if (Test-Path ".\tools\rclone.exe") { return (Resolve-Path ".\tools\rclone.exe").Path }
  if (Test-Path "..\tools\rclone.exe") { return (Resolve-Path "..\tools\rclone.exe").Path }
  $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter rclone.exe -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  if ($found) { return $found }
  if (Test-Path "$env:LOCALAPPDATA\rclone\rclone.exe") { return "$env:LOCALAPPDATA\rclone\rclone.exe" }
  if (Test-Path "C:\Program Files\rclone\rclone.exe") { return "C:\Program Files\rclone\rclone.exe" }
  return $null
}

function Test-WinFsp {
  if (Test-Path "${env:ProgramFiles(x86)}\WinFsp\bin\winfsp-x64.dll") { return $true }
  if (Test-Path "$env:ProgramFiles\WinFsp\bin\winfsp-x64.dll") { return $true }
  $apps = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue
  return [bool]($apps | Where-Object { $_.DisplayName -like "WinFsp*" } | Select-Object -First 1)
}

function Install-RcloneDirect {
  Write-Host "Baixando rclone oficial portátil..."
  $zipUrl = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
  $tempZip = Join-Path $env:TEMP "rclone_temp.zip"
  $tempExtract = Join-Path $env:TEMP "rclone_extract"
  
  if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue }
  
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $zipUrl -OutFile $tempZip -UseBasicParsing
  Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force
  
  $extractedExe = Get-ChildItem $tempExtract -Filter rclone.exe -Recurse | Select-Object -First 1 -ExpandProperty FullName
  if ($extractedExe) {
    if (-not (Test-Path ".\tools")) { New-Item -ItemType Directory -Path ".\tools" | Out-Null }
    Copy-Item -Path $extractedExe -Destination ".\tools\rclone.exe" -Force
    Write-Host "rclone instalado em .\tools\rclone.exe com sucesso."
  }
  Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
  Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue
}

function Install-WinFspDirect {
  Write-Host "Baixando instalador WinFsp..."
  $msiUrl = "https://github.com/winfsp/winfsp/releases/download/v2.0/winfsp-2.0.23075.msi"
  $tempMsi = Join-Path $env:TEMP "winfsp.msi"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $msiUrl -OutFile $tempMsi -UseBasicParsing
  Write-Host "Instalando WinFsp..."
  Start-Process msiexec.exe -ArgumentList "/i `"$tempMsi`" /quiet /qn /norestart" -Wait
  Remove-Item $tempMsi -Force -ErrorAction SilentlyContinue
}

# 1. Garante WinFsp
if (-not (Test-WinFsp)) {
  if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
    try {
      Write-Host "Instalando WinFsp via winget..."
      winget install --id "WinFsp.WinFsp" --silent --accept-package-agreements --accept-source-agreements
    } catch {
      Install-WinFspDirect
    }
  } else {
    Install-WinFspDirect
  }
}

# 2. Garante Rclone
$rclone = Find-Rclone
if (-not $rclone) {
  # Tenta primeiro script python de instalação se python estiver disponível
  if (Get-Command python.exe -ErrorAction SilentlyContinue) {
    python.exe .\tools\rclone_installer.py
    $rclone = Find-Rclone
  }
  if (-not $rclone) {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
      try {
        Write-Host "Instalando rclone via winget..."
        winget install --id "Rclone.Rclone" --silent --accept-package-agreements --accept-source-agreements
        $rclone = Find-Rclone
      } catch {
        Install-RcloneDirect
        $rclone = Find-Rclone
      }
    } else {
      Install-RcloneDirect
      $rclone = Find-Rclone
    }
  }
}

if (-not $rclone) { throw "rclone nao encontrado apos tentativa de instalacao automatica." }

$targetDrive = "N"
if (Get-PSDrive N -ErrorAction SilentlyContinue) {
  $rcloneMountN = Get-CimInstance Win32_Process -Filter "name = 'rclone.exe'" |
    Where-Object { $_.CommandLine -like "*mount nebula:*" -and $_.CommandLine -like "*N:*" } |
    Select-Object -First 1
  if ($rcloneMountN) {
    Write-Host "N: ja esta montado pelo rclone."
    exit 0
  }
  $targetDrive = "Z"
}

if (Get-PSDrive $targetDrive -ErrorAction SilentlyContinue) {
  $rcloneMountZ = Get-CimInstance Win32_Process -Filter "name = 'rclone.exe'" |
    Where-Object { $_.CommandLine -like "*mount nebula:*" -and $_.CommandLine -like "*$($targetDrive):*" } |
    Select-Object -First 1
  if ($rcloneMountZ) {
    Write-Host "$($targetDrive): ja esta montado pelo rclone."
    exit 0
  }
  throw "$($targetDrive): ja esta em uso por outro programa. Desmonte o mapeamento existente antes de iniciar."
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
& $rclone mount nebula:/ "$($targetDrive):" `
  --config "$configFile" `
  --vfs-cache-mode full `
  --vfs-cache-max-size 20G `
  --dir-cache-time 10s `
  --poll-interval 0 `
  --log-file ".\rclone-mount.log" `
  --log-level INFO
