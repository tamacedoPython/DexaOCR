<#
.SYNOPSIS
    Configura o acesso SMB do DexaOCRWorker executado como LocalSystem.

.DESCRIPTION
    Armazena as credenciais dos servidores de arquivos no perfil LocalSystem,
    abre as conexoes SMB, valida a leitura de um DICOM e reinicia o servico.
    Execute este script em um PowerShell aberto como Administrador.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# Use as credenciais DA REDE/NAS, nao as do administrador deste PC.
$UsuarioRede = 'TELEPACS\Administrador'
$SenhaRede   = '2028?Avant!@gil3#.'

$NomeServico = 'DexaOCRWorker'
$StudyUid = '1.2.840.113850.237188128021080174017008189155141231075171253233'

$Servidores = @('192.168.1.155', '192.168.1.60')
$Compartilhamentos = @(
    '\\192.168.1.155\dcms_new_ssd_01'
    '\\192.168.1.155\Dados'
    '\\192.168.1.60\dcms'
)
$RaizesDicom = @(
    '\\192.168.1.155\dcms_new_ssd_01\DCMs'
    '\\192.168.1.155\Dados\Database\Dcms'
    '\\192.168.1.60\dcms'
)
$PastaEstudoTeste = "\\192.168.1.155\dcms_new_ssd_01\DCMs\2026\09\14\$StudyUid"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Abra o PowerShell como Administrador e execute novamente.'
    }
}

function Assert-Configuration {
    if ([string]::IsNullOrWhiteSpace($UsuarioRede) -or
        $UsuarioRede -eq 'COLOQUE USUARIO AQUI') {
        throw 'Preencha $UsuarioRede com o usuario da rede/NAS.'
    }
    if ([string]::IsNullOrWhiteSpace($SenhaRede) -or
        $SenhaRede -eq 'COLOQUE SENHA AQUI') {
        throw 'Preencha $SenhaRede com a senha da rede/NAS.'
    }

    $servico = Get-CimInstance Win32_Service -Filter "Name='$NomeServico'"
    if (-not $servico) {
        throw "O servico '$NomeServico' nao esta instalado."
    }
    if ($servico.StartName -notin @('LocalSystem', 'LocalSystemAccount')) {
        throw "O servico executa como '$($servico.StartName)', nao como LocalSystem."
    }
}

function ConvertTo-SingleQuotedLiteral {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

Assert-Administrator
Assert-Configuration

$id = [Guid]::NewGuid().ToString('N')
$nomeTarefa = "DexaOCR-ConfigurarSMB-$id"
$pastaTemporaria = Join-Path $env:ProgramData 'DexaOCR'
$arquivoResultado = Join-Path $pastaTemporaria "configurar-smb-$id.txt"
[IO.Directory]::CreateDirectory($pastaTemporaria) | Out-Null

$usuarioLiteral = ConvertTo-SingleQuotedLiteral $UsuarioRede
$senhaLiteral = ConvertTo-SingleQuotedLiteral $SenhaRede
$resultadoLiteral = ConvertTo-SingleQuotedLiteral $arquivoResultado
$estudoLiteral = ConvertTo-SingleQuotedLiteral $PastaEstudoTeste
$servidoresCodigo = ($Servidores | ForEach-Object {
    '    ' + (ConvertTo-SingleQuotedLiteral $_)
}) -join ",`r`n"
$compartilhamentosCodigo = ($Compartilhamentos | ForEach-Object {
    '    ' + (ConvertTo-SingleQuotedLiteral $_)
}) -join ",`r`n"
$raizesCodigo = ($RaizesDicom | ForEach-Object {
    '    ' + (ConvertTo-SingleQuotedLiteral $_)
}) -join ",`r`n"

# Este bloco sera executado realmente como NT AUTHORITY\SYSTEM.
$codigoSystem = @"
`$ErrorActionPreference = 'Stop'
`$usuario = $usuarioLiteral
`$senha = $senhaLiteral
`$arquivoResultado = $resultadoLiteral
`$pastaEstudo = $estudoLiteral
`$servidores = @(
$servidoresCodigo
)
`$compartilhamentos = @(
$compartilhamentosCodigo
)
`$raizes = @(
$raizesCodigo
)

try {
    # Encerra conexoes antigas do LocalSystem para evitar conflito SMB 1219.
    foreach (`$compartilhamento in `$compartilhamentos) {
        & net.exe use `$compartilhamento /delete /y 2>`$null | Out-Null
    }

    # Persiste a credencial no Gerenciador de Credenciais do LocalSystem.
    foreach (`$servidor in `$servidores) {
        & cmdkey.exe "/delete:`$servidor" 2>`$null | Out-Null
        & cmdkey.exe "/add:`$servidor" "/user:`$usuario" "/pass:`$senha" | Out-Null
        if (`$LASTEXITCODE -ne 0) {
            throw "Falha ao salvar credencial para `$servidor. Codigo: `$LASTEXITCODE"
        }
    }

    # Abre conexoes SMB sem depender de letras de unidade.
    foreach (`$compartilhamento in `$compartilhamentos) {
        & net.exe use `$compartilhamento `$senha "/user:`$usuario" /persistent:yes | Out-Null
        if (`$LASTEXITCODE -ne 0) {
            throw "Falha ao conectar em `$compartilhamento. Codigo: `$LASTEXITCODE"
        }
    }

    foreach (`$raiz in `$raizes) {
        if (-not (Test-Path -LiteralPath `$raiz -PathType Container)) {
            throw "Sem acesso a raiz: `$raiz"
        }
    }
    if (-not (Test-Path -LiteralPath `$pastaEstudo -PathType Container)) {
        throw "Sem acesso a pasta do estudo: `$pastaEstudo"
    }

    `$dicoms = @(Get-ChildItem -LiteralPath `$pastaEstudo -Filter '*.dcm' -File)
    if (`$dicoms.Count -eq 0) {
        throw "Nenhum DICOM encontrado em: `$pastaEstudo"
    }

    foreach (`$dicom in `$dicoms) {
        `$stream = [IO.File]::Open(
            `$dicom.FullName,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        try {
            `$buffer = New-Object byte[] 132
            if (`$stream.Read(`$buffer, 0, `$buffer.Length) -le 0) {
                throw "Nao foi possivel ler: `$(`$dicom.FullName)"
            }
        }
        finally {
            `$stream.Dispose()
        }
    }

    `$texto = @(
        'RESULTADO=SUCESSO'
        "IDENTIDADE=`$([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
        "DICOMS_LIDOS=`$(`$dicoms.Count)"
    )
    [IO.File]::WriteAllLines(`$arquivoResultado, `$texto, [Text.UTF8Encoding]::new(`$false))
    exit 0
}
catch {
    [IO.File]::WriteAllText(
        `$arquivoResultado,
        "RESULTADO=ERRO`r`nMENSAGEM=`$(`$_.Exception.Message)",
        [Text.UTF8Encoding]::new(`$false)
    )
    exit 1
}
"@

$codigoCodificado = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($codigoSystem))
$acao = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $codigoCodificado"

$resultado = $null
try {
    Write-Host 'Configurando e testando o SMB como LocalSystem...' -ForegroundColor Cyan
    Register-ScheduledTask `
        -TaskName $nomeTarefa `
        -Action $acao `
        -User 'SYSTEM' `
        -RunLevel Highest `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $nomeTarefa

    $limite = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        $estado = (Get-ScheduledTask -TaskName $nomeTarefa).State
    }
    while ($estado -eq 'Running' -and (Get-Date) -lt $limite)

    if ($estado -eq 'Running') {
        throw 'A configuracao excedeu o limite de 60 segundos.'
    }
    if (-not (Test-Path -LiteralPath $arquivoResultado -PathType Leaf)) {
        throw 'A tarefa LocalSystem terminou sem produzir um resultado.'
    }
    $resultado = [IO.File]::ReadAllText($arquivoResultado)
}
finally {
    Unregister-ScheduledTask -TaskName $nomeTarefa -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $arquivoResultado -Force -ErrorAction SilentlyContinue
    $SenhaRede = $null
    $senhaLiteral = $null
}

Write-Host "`n$resultado"
if ($resultado -notmatch '^RESULTADO=SUCESSO') {
    Write-Host "`nConfiguracao nao concluida." -ForegroundColor Red
    exit 1
}

Write-Host "`nReiniciando $NomeServico..." -ForegroundColor Cyan
Restart-Service -Name $NomeServico -Force
(Get-Service -Name $NomeServico).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
Write-Host 'SUCESSO: o LocalSystem leu os DICOMs e o worker foi reiniciado.' -ForegroundColor Green
Write-Host 'Apague este script depois do uso para remover a senha em texto simples.' -ForegroundColor Yellow
exit 0
