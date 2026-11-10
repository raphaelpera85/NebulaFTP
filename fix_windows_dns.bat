@echo off
:: Script para configurar DNS do Google (IPv4 e IPv6) na interface Ethernet
:: Exige permissões de Administrador
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Solicitando privilegios de Administrador...
    powershell -Command "Start-Process '%~0' -Verb RunAs"
    exit /b
)

echo Configurando DNS IPv4 e IPv6 (Google DNS)...
powershell -NoProfile -Command "Set-DnsClientServerAddress -InterfaceAlias 'Ethernet' -ServerAddresses ('8.8.8.8','8.8.4.4','2001:4860:4860::8888','2001:4860:4860::8844') -ErrorAction SilentlyContinue"
powershell -NoProfile -Command "Clear-DnsClientCache"

echo.
echo DNS configurado com sucesso:
powershell -NoProfile -Command "Get-DnsClientServerAddress -InterfaceAlias 'Ethernet'"
echo.
pause
