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

`python -m pytest -q`: 144 testes passaram no ambiente de desenvolvimento.

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
O PaddleOCR real e o serviço Windows não foram executados neste ambiente.
Nenhum dado identificável de paciente foi incluído nas fixtures.

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
