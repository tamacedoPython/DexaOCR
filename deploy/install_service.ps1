<#
.SYNOPSIS
    Instala o DexaOCRWorker como servico nativo do Windows.

.DESCRIPTION
    Registra o mesmo DexaOCRWorker.exe em modo --service, configura inicio
    automatico e reinicio em caso de falha. Nao requer NSSM.

.PARAMETER InstallDir
    Pasta que contem DexaOCRWorker.exe. Por padrao, usa a pasta deste script.

.PARAMETER ServiceName
    Nome interno do servico. Padrao: DexaOCRWorker.

.PARAMETER Credential
    Credencial opcional para executar o servico. Sem este parametro, usa
    LocalSystem. Use uma conta com acesso aos compartilhamentos DICOM de rede.

.PARAMETER NoStart
    Instala sem iniciar imediatamente.

.EXAMPLE
    .\install_service.ps1

.EXAMPLE
    .\install_service.ps1 -Credential (Get-Credential)
#>
param(
    [string]$InstallDir = $PSScriptRoot,
    [string]$ServiceName = "DexaOCRWorker",
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "Execute este script em um PowerShell aberto como Administrador."
}

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$ExePath = Join-Path $InstallDir "DexaOCRWorker.exe"
if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
    throw "DexaOCRWorker.exe nao encontrado em: $ExePath"
}

$EnvFile = Join-Path $InstallDir ".env"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    Write-Warning "Arquivo .env nao encontrado em $EnvFile. Configure-o antes de iniciar o servico."
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removendo instalacao anterior de '$ServiceName'..." -ForegroundColor Yellow
    if ($existing.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force
        $existing.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    & sc.exe delete $ServiceName | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Nao foi possivel remover o servico anterior." }
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        throw "O Windows ainda nao concluiu a remocao do servico. Tente novamente em alguns segundos."
    }
}

$binaryPath = '"{0}" --service' -f $ExePath
$serviceParams = @{
    Name = $ServiceName
    BinaryPathName = $binaryPath
    DisplayName = "DexaOCR Worker"
    Description = "Processa estudos DICOM DXA via RabbitMQ para o Orchestra."
    StartupType = "Automatic"
}
if ($Credential) {
    $serviceParams.Credential = $Credential
}

Write-Host "Instalando servico '$ServiceName'..." -ForegroundColor Cyan
New-Service @serviceParams | Out-Null

& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Servico criado, mas a politica de recuperacao nao foi configurada." }
& sc.exe failureflag $ServiceName 1 | Out-Host

if (-not $NoStart) {
    Write-Host "Iniciando servico..." -ForegroundColor Cyan
    Start-Service -Name $ServiceName
}

$service = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "Servico instalado com sucesso." -ForegroundColor Green
Write-Host "Nome       : $ServiceName"
Write-Host "Executavel : $ExePath"
Write-Host "Status     : $($service.Status)"
Write-Host "Log        : $(Join-Path $InstallDir 'logs\dexa_worker.log')"
Write-Host ""
Write-Host "Modo interativo: execute DexaOCRWorker.exe sem argumentos." -ForegroundColor Cyan
