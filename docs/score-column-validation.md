# Correção de associação entre T-score e Z-score

O parser GE antigo sempre usava a terceira célula numérica como T-score.
Isso funciona em DMO/%JA/T/%AM/Z, mas lê Z como T no formato DMO/T/Z.

## Alteração

- Tesseract e PaddleOCR preservam caixas e confiança de cada item reconhecido.
- Tabelas com moldura são localizadas na página completa, incluindo o cabeçalho
  acima do recorte antigo de 44% da altura. Relatórios sem moldura usam OCR
  posicional da página completa.
- Cabeçalhos BMD/DMO, T-score/Escore T e Z-score/Escore Z definem as colunas.
  Percentuais são opcionais; PR e AM são identificados separadamente.
- Valores são associados por posição e convertidos para cinco posições
  canônicas, com marcadores explícitos para células ausentes. A ausência de uma
  célula não desloca a seguinte.
- Cabeçalhos ambíguos, células ilegíveis ou regiões não reconhecidas geram
  avisos em `ocr_metadata.warnings`. Uma tabela não reconhecida não fornece
  scores por fallback posicional.
- Chamadas textuais legadas rejeitam linhas com contagem incompatível; o formato
  compacto exige cabeçalho explícito ou o parâmetro `ge_compact`.

O contrato JSON dos valores numéricos permanece igual. `None` é serializado
como `null`. Os avisos devem ser tratados pelo consumidor como indicação de
revisão; esta alteração não cria um novo status no protocolo RabbitMQ.

## Validação realizada

`python -m pytest -q`: 151 testes passaram no ambiente de desenvolvimento.

Cobertura nova: tabelas compactas e com percentuais; mudanças de escala e
posição; colunas T/Z reordenadas; scores positivos; células ausentes ou de baixa
confiança; sinais separados; cabeçalhos ausentes/duplicados; seção de tendência;
DMO/CMO e PR/AM; APIs de caixas PaddleOCR v2/v3 com respostas simuladas; integração
do pipeline; Tesseract real em tabelas sintéticas sem dados de pacientes.

As duas capturas de tela fornecidas foram inspecionadas e usadas em testes
locais. O OCR Tesseract ainda apresentou erros de caracteres nessas imagens de
baixa resolução, especialmente sinais de menos e alguns dígitos. Portanto,
os testes acima demonstram a correção da associação de colunas, mas **não
certificam a transcrição completa dos anexos nem de equipamentos arbitrários**.
Na etapa inicial, o PaddleOCR real não havia sido executado. A validação com
DICOMs originais abaixo resolve essa pendência para os layouts recebidos.
O serviço Windows não foi executado neste ambiente.
Nenhum dado identificável de paciente foi incluído nas fixtures.

## Validação adicional com DICOMs originais — 2026-10-02

Foram recebidos 18 DICOMs de três estudos, abrangendo GE Lunar com tabelas
compactas, GE Lunar com percentuais e Hologic Horizon. São 10 páginas com
tabelas (incluindo duplicatas e resultados auxiliares), seis páginas de
documentos/autorizações e duas imagens anatômicas sem tabela.

Ambiente: Linux, Python 3.12, PaddleOCR 2.10.0, PaddlePaddle 2.6.2, idioma `pt`,
modelos locais PP-OCRv3. Não houve envio das imagens a uma API externa.

Foi conferida manualmente uma transcrição de referência das tabelas. A leitura
numérica foi comparada com essa referência tanto na resolução nativa quanto
após `dicom_to_pil(..., resize_width=2200)`, a conversão padrão do projeto.
Em cada execução: **61 linhas, 249 valores numéricos, zero divergências**.
Também foram conferidos os campos nulos; os documentos e imagens sem tabela
não produziram medições. O escopo é DMO, T-score, Z-score, %JA/PR e %AM;
área, CMO, largura/altura e informações administrativas não integram essa métrica.

Essa validação revelou e corrigiu:

- Grades Hologic claras sobre fundo cinza, que a busca apenas por bordas
  escuras não localizava.
- Cabeçalhos `Escore` e `T/Z` em linhas separadas, `PR (pico padrão)` e
  `AM (pareado por idade)`, além de valores alinhados à direita.
- `CM0` no cabeçalho CMO e `12/13/14` no lugar de L2/L3/L4. A recuperação
  dessas vértebras exige evidência de coluna lombar e alinhamento da região.
- Rotação indevida de caixas numéricas pelo classificador de orientação do
  PaddleOCR. A leitura posicional de tabelas já orientadas usa `cls=False`;
  a leitura de texto comum mantém a classificação de orientação anterior.
  Tokens numéricos invertidos são recusados, sem tentar adivinhar os dígitos.
- Dois números dentro da mesma janela não são concatenados.
- Páginas sem tabela não são mais escolhidas como fonte do cabeçalho do
  paciente no CLI ou worker. Os avisos continuam disponíveis nos metadados.

Persistem avisos conservadores quando o OCR não detecta os traços de campos
não informados, como os scores da diáfise; os campos permanecem `null`.
Os DICOMs, imagens, identificadores e caches de OCR não foram incluídos no Git.

Esta etapa não executou SQL, RabbitMQ nem o serviço Windows. Também não
certifica layouts ainda não fornecidos, outras versões do OCR ou todos os
campos demográficos. Antes da implantação, conferir a versão instalada e
realizar teste no ambiente Windows com os mesmos DICOMs.

## Próxima validação antes de produção

1. Usar DICOM ou PNG original, na resolução recebida pelo worker, de cada
   equipamento/layout; conferir DMO, T, Z e percentuais em todas as regiões.
2. Executar com a mesma versão/configuração do PaddleOCR do serviço.
3. Incluir coluna lombar, fêmur, antebraço, valores positivos, negativos e vazios.
4. Verificar os avisos e qualquer perda de linhas; layouts novos devem ganhar
   fixtures próprias antes de serem considerados validados.
5. Após validação/merge, recompilar o executável com o fluxo de build existente
   e atualizar o serviço. Atualizar o Git sozinho não modifica o EXE instalado.

Limitações deliberadas: a leitura atual requer uma tabela única com cabeçalhos
DMO, T e Z reconhecíveis. Tabelas sem um desses cabeçalhos, páginas com múltiplas
tabelas, regiões ou disposições não suportadas exigem revisão/extensão. A
confiança do OCR não é garantia de correção de caracteres; conferir com a fonte.
