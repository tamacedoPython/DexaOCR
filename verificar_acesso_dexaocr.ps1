<#
.SYNOPSIS
    Verifica se o DexaOCRWorker consegue ler os DICOMs como LocalSystem.

.DESCRIPTION
    Cria uma tarefa agendada temporaria executada com a mesma identidade do
    servico DexaOCRWorker. Para contas diferentes de LocalSystem, solicita a
    senha de forma oculta. O teste confirma a existencia da pasta, lista os
    arquivos .dcm e abre cada arquivo para leitura. A tarefa e o arquivo
    temporario de resultado sao removidos ao final.

    Execute em um PowerShell aberto como Administrador.
#>

[CmdletBinding()]
param(
    [string]$StudyUid = '1.2.840.113850.237188128021080174017008189155141231075171253233',
    [string]$DicomRoot = '\\192.168.1.155\dcms_new_ssd_01\DCMs'
)

$ErrorActionPreference = 'Stop'
$NomeServico = 'DexaOCRWorker'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)

    if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw 'Abra o PowerShell como Administrador e execute novamente.'
    }
}

Assert-Administrator

Write-Host 'Versao do verificador: 3' -ForegroundColor Cyan

$servico = Get-CimInstance Win32_Service -Filter "Name='$NomeServico'"
if (-not $servico) {
    throw "O servico '$NomeServico' nao esta instalado."
}

Write-Host 'Servico encontrado:' -ForegroundColor Cyan
$servico | Select-Object Name, State, StartName, PathName | Format-List

$usaLocalSystem = $servico.StartName -in @('LocalSystem', 'LocalSystemAccount')
$usuarioTarefa = 'SYSTEM'
$senhaTarefa = $null

if (-not $usaLocalSystem) {
    if ($servico.StartName.StartsWith('.\')) {
        $usuarioTarefa = "$env:COMPUTERNAME\$($servico.StartName.Substring(2))"
    }
    else {
        $usuarioTarefa = $servico.StartName
    }

    Write-Host "O teste sera executado como: $usuarioTarefa" -ForegroundColor Yellow
    $senhaSegura = Read-Host "Digite a senha Windows de $usuarioTarefa" -AsSecureString
    $ponteiroSenha = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senhaSegura)
    try {
        $senhaTarefa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ponteiroSenha)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ponteiroSenha)
        $senhaSegura = $null
    }

    if ([string]::IsNullOrEmpty($senhaTarefa)) {
        throw 'A senha da conta do servico nao pode ficar vazia.'
    }
}

$pastaEstudo = Join-Path $DicomRoot (Join-Path '2026\09\14' $StudyUid)
$id = [Guid]::NewGuid().ToString('N')
$nomeTarefa = "DexaOCR-TesteAcesso-$id"
$pastaTemporaria = Join-Path $env:ProgramData 'DexaOCR'
$arquivoResultado = Join-Path $pastaTemporaria "teste-acesso-$id.txt"

[IO.Directory]::CreateDirectory($pastaTemporaria) | Out-Null

$pastaEscapada = $pastaEstudo.Replace("'", "''")
$resultadoEscapado = $arquivoResultado.Replace("'", "''")

# Este bloco sera executado pelo Agendador com a identidade real do servico.
$codigoSystem = @"
`$ErrorActionPreference = 'Stop'
`$pastaEstudo = '$pastaEscapada'
`$arquivoResultado = '$resultadoEscapado'

try {
    if (-not [IO.Directory]::Exists(`$pastaEstudo)) {
        throw "Pasta nao encontrada ou sem permissao: `$pastaEstudo"
    }

    `$arquivos = @([IO.Directory]::GetFiles(
        `$pastaEstudo,
        '*.dcm',
        [IO.SearchOption]::TopDirectoryOnly
    ))

    if (`$arquivos.Count -eq 0) {
        throw "Nenhum arquivo .dcm encontrado em: `$pastaEstudo"
    }

    `$linhas = [Collections.Generic.List[string]]::new()
    `$linhas.Add('VERSAO_TESTE=3')
    `$linhas.Add('RESULTADO=SUCESSO')
    `$linhas.Add("IDENTIDADE=`$([Security.Principal.WindowsIdentity]::GetCurrent().Name)")
    `$linhas.Add("PASTA=`$pastaEstudo")
    `$linhas.Add("QUANTIDADE=`$(`$arquivos.Count)")

    foreach (`$caminhoArquivo in `$arquivos) {
        # Abrir o arquivo comprova permissao de leitura, nao apenas de listagem.
        `$stream = [IO.File]::Open(
            `$caminhoArquivo,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )

        try {
            `$buffer = New-Object byte[] 132
            `$bytesLidos = `$stream.Read(`$buffer, 0, `$buffer.Length)

            if (`$bytesLidos -le 0) {
                throw "O arquivo foi aberto, mas nenhum byte foi lido: `$caminhoArquivo"
            }
        }
        finally {
            `$stream.Dispose()
        }

        `$tamanho = [IO.FileInfo]::new(`$caminhoArquivo).Length
        `$linhas.Add("ARQUIVO_OK=`$caminhoArquivo | `$tamanho bytes")
    }

    [IO.File]::WriteAllLines(
        `$arquivoResultado,
        `$linhas,
        [Text.UTF8Encoding]::new(`$false)
    )

    exit 0
}
catch {
    [IO.File]::WriteAllText(
        `$arquivoResultado,
        "VERSAO_TESTE=3`r`nRESULTADO=ERRO`r`nMENSAGEM=`$(`$_.Exception.Message)",
        [Text.UTF8Encoding]::new(`$false)
    )

    exit 1
}
"@

$codigoCodificado = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($codigoSystem)
)

$acao = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $codigoCodificado"

$resultadoTeste = $null

try {
    Write-Host "Testando como $usuarioTarefa`: $pastaEstudo" -ForegroundColor Cyan

    if ($usaLocalSystem) {
        Register-ScheduledTask `
            -TaskName $nomeTarefa `
            -Action $acao `
            -User 'SYSTEM' `
            -RunLevel Highest `
            -Force | Out-Null
    }
    else {
        Register-ScheduledTask `
            -TaskName $nomeTarefa `
            -Action $acao `
            -User $usuarioTarefa `
            -Password $senhaTarefa `
            -RunLevel Highest `
            -Force | Out-Null
    }

    Start-ScheduledTask -TaskName $nomeTarefa

    $limite = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        $estadoTarefa = (Get-ScheduledTask -TaskName $nomeTarefa).State
    }
    while ($estadoTarefa -eq 'Running' -and (Get-Date) -lt $limite)

    if ($estadoTarefa -eq 'Running') {
        throw 'O teste excedeu o limite de 60 segundos.'
    }

    if (-not (Test-Path -LiteralPath $arquivoResultado -PathType Leaf)) {
        $info = Get-ScheduledTaskInfo -TaskName $nomeTarefa
        throw "A tarefa nao produziu resultado. Codigo: $($info.LastTaskResult)"
    }

    $resultadoTeste = [IO.File]::ReadAllText($arquivoResultado)
}
finally {
    Unregister-ScheduledTask `
        -TaskName $nomeTarefa `
        -Confirm:$false `
        -ErrorAction SilentlyContinue

    Remove-Item `
        -LiteralPath $arquivoResultado `
        -Force `
        -ErrorAction SilentlyContinue

    $senhaTarefa = $null
}

Write-Host "`n$resultadoTeste"

if ($resultadoTeste -notmatch '(?m)^RESULTADO=SUCESSO\s*$') {
    Write-Host "`nFALHA: o DexaOCR nao possui acesso confirmado aos DICOMs." `
        -ForegroundColor Red
    exit 1
}

Write-Host "`nSUCESSO: o DexaOCR consegue listar e ler os arquivos DICOM." `
    -ForegroundColor Green
exit 0
