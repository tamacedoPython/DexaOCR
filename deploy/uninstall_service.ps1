<# Remove o servico nativo DexaOCRWorker. Execute como Administrador. #>
param([string]$ServiceName = "DexaOCRWorker")

$ErrorActionPreference = 'Stop'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) { throw "Execute este script em um PowerShell aberto como Administrador." }

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Warning "Servico '$ServiceName' nao encontrado."
    exit 0
}
if ($service.Status -ne 'Stopped') {
    Stop-Service -Name $ServiceName -Force
    $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
}
& sc.exe delete $ServiceName | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Nao foi possivel remover o servico '$ServiceName'." }
Write-Host "Servico '$ServiceName' removido." -ForegroundColor Green
