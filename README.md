# DexaOCR Worker

DexaOCR e um **Worker especializado** da plataforma Orchestra, responsavel por processar exames de densitometria ossea (DXA/Dexa) via OCR e retornar um JSON estruturado.

Faz parte de uma arquitetura de Workers medicos orquestrados onde:
- **Orchestra** distribui requisicoes via RabbitMQ
- **DexaOCR Worker** consome, processa e responde
- **EchoMind** consome a resposta para geracao de pre-laudos com LLM

---

## Visao Arquitetural

```
Orchestra
    |
    |  [RabbitMQ: worker.dexaocr.request]
    v
DexaOCR Worker
    +-- consumer (RabbitMQ)
    +-- validation (Pydantic)
    +-- dexa_processing_service (OCR pipeline)
    +-- repository (AgileAI DB)
    +-- publisher (RabbitMQ)
    |
    |  [RabbitMQ: orchestra.worker_results]
    v
EchoMind (LLM pre-laudo)
```

---

## Fluxo Ponta a Ponta

1. Orchestra publica mensagem na fila `worker.dexaocr.request`
2. DexaOCR Worker consome e valida o contrato (Pydantic)
3. Registra recebimento no AgileAI (`WorkerRequests` + `WorkerEvents`)
4. Localiza arquivos DICOM no servidor via StudyUID
5. Converte DICOMs para PNG (isolados por request_id em `work/`)
6. Executa OCR (Tesseract ou PaddleOCR) com ROI segmentado
7. Parseia header, tabelas e comentarios -> JSON estruturado
8. Registra resultado no AgileAI
9. Publica resposta na fila `orchestra.worker_results`
10. Registra publicacao no AgileAI
11. Envia ACK para o RabbitMQ

Em caso de erro: registra no banco, publica resposta de erro padronizada, e adota politica de ACK/NACK consistente.

---

## Estrutura do Projeto

```
DexaOCR/
+-- worker_main.py             # Ponto de entrada do Worker
+-- main.py                    # Modo CLI legado (preservado)
+-- .env.example               # Template de configuracao
+-- requirements.txt
+-- database/
|   +-- agileai_worker_tables.sql   # DDL das tabelas no AgileAI
+-- src/
|   +-- dexa_ocr/              # Pipeline OCR original (preservado)
|   |   +-- config.py
|   |   +-- db.py
|   |   +-- pipeline.py
|   |   +-- dicom_locator.py
|   |   +-- dicom_to_png.py
|   |   +-- models/
|   |   +-- services/
|   |   +-- utils/
|   +-- worker/                # Infraestrutura do Worker
|       +-- config/settings.py       # WorkerSettings (le .env)
|       +-- models/contracts.py      # WorkerRequest / WorkerResponse (Pydantic)
|       +-- models/db_entities.py    # Entidades do banco
|       +-- messaging/connection.py  # RabbitMQConnection (reconexao automatica)
|       +-- messaging/consumer.py    # RabbitMQConsumer (loop blocking)
|       +-- messaging/publisher.py   # RabbitMQPublisher
|       +-- db/connection.py         # WorkerDBConnection (SQL Server)
|       +-- repositories/worker_repository.py  # SQL isolado
|       +-- services/worker_service.py          # WorkerOrchestrationService
|       +-- services/dexa_processing_service.py # DexaOCRProcessingService
+-- tests/
    +-- test_worker.py         # Testes da camada Worker
    +-- test_models.py
    +-- test_parser_header.py
    +-- test_parser_table.py
    +-- test_text_utils.py
```

---

## Banco de Dados AgileAI

Duas tabelas criadas via `database/agileai_worker_tables.sql`:

### `dbo.WorkerRequests`
Uma linha por requisicao. Atualizada conforme o ciclo avanca.

| Coluna | Descricao |
|---|---|
| RequestID | UUID unico da requisicao |
| CorrelationID | ID de rastreamento cross-service |
| WorkerName | "DexaOCR" (escalavel para outros workers) |
| StudyUID | DICOM Study UID |
| Status | RECEIVED -> PROCESSING -> SUCCESS/ERROR -> PUBLISHED |
| ProcessingTimeMs | Duracao do processamento em ms |
| OutputJson | JSON estruturado completo |
| ErrorType / ErrorMessage | Detalhes do erro se houver |
| PublishedToRabbitMQ | Flag de publicacao |

### `dbo.WorkerEvents`
Trilha de auditoria imutavel. Uma linha por evento por requisicao.

| Coluna | Descricao |
|---|---|
| Stage | Etapa: MESSAGE_RECEIVED, PROCESSING_STARTED, etc. |
| Status | OK / FAILED / INFO / WARNING |
| Message | Mensagem legivel |
| Details | Detalhes tecnicos |
| EventAt | Timestamp com timezone |

---

## Formato das Mensagens

### Request (Orchestra -> DexaOCR)

```json
{
  "schema_version": "1.0",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "correlation_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "source_system": "Orchestra",
  "target_worker": "DexaOCR",
  "requested_at": "2026-04-23T10:00:00Z",
  "priority": 5,
  "retry_count": 0,
  "callback_queue": "orchestra.worker_results",
  "payload": {
    "study_uid": "1.2.840.10008.5.1.4.1.1.9999",
    "accession_number": "ACC-2026-001",
    "patient_id": "PAT-12345",
    "modality": "DXA",
    "exam_type": "Densitometria Ossea",
    "parameters": {
      "language": "por",
      "return_debug_data": false
    }
  }
}
```

### Response - Sucesso

```json
{
  "schema_version": "1.0",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "correlation_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "worker": "DexaOCR",
  "processed_at": "2026-04-23T10:00:05Z",
  "status": "success",
  "error": null,
  "metrics": {
    "processing_time_ms": 4231,
    "pages_processed": 2,
    "pages_skipped": 2,
    "ocr_engine": "paddleocr"
  },
  "input": { "study_uid": "1.2.840.10008.5.1.4.1.1.9999" },
  "output": {
    "exam_type": "Densitometria Ossea",
    "structured_data": {
      "patient_info": { "name": "JOAO SILVA", "age": "55.0" },
      "lumbar_spine": { "L1": { "bmd": 0.732, "t_score": -3.3 } },
      "right_femur": { "Colo": { "bmd": 0.653, "t_score": -2.1 } },
      "comments": null
    }
  }
}
```

### Response - Erro

```json
{
  "schema_version": "1.0",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "correlation_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "worker": "DexaOCR",
  "processed_at": "2026-04-23T10:00:02Z",
  "status": "error",
  "error": {
    "type": "DexaProcessingError",
    "message": "Falha ao processar exame DXA",
    "details": "No DICOM files found for study_uid=1.2.840.9999"
  },
  "metrics": { "processing_time_ms": 312 },
  "input": { "study_uid": "1.2.840.9999" },
  "output": null
}
```

---

## Como Executar Localmente

### Pre-requisitos
- Python 3.11+
- RabbitMQ rodando em localhost:5672
- SQL Server com banco AgileAI acessivel
- Tesseract instalado (ou PaddleOCR configurado)

### Instalacao
```bash
pip install -r requirements.txt
```

### Configuracao
```bash
cp .env.example .env
# Editar .env com suas credenciais
```

### Criar tabelas no banco
```bash
sqlcmd -S 192.168.1.211 -d AgileAI -U sasys -P senha -i database/agileai_worker_tables.sql
```

### Iniciar o Worker
```bash
python worker_main.py
```

### Executavel Windows: console e servico

O executavel compilado possui dois modos:

```powershell
# Modo interativo, com logs visiveis no terminal
.\DexaOCRWorker.exe

# Instalar e iniciar como servico (PowerShell como Administrador)
.\install_service.ps1

# Para compartilhamentos de rede, configure no .env:
# DICOM_NETWORK_USERNAME=DOMINIO\usuario
# DICOM_NETWORK_PASSWORD=senha
```

O instalador registra `DexaOCRWorker.exe --service`, configura inicio automatico
e tres tentativas de reinicio apos falha. O modo servico grava o log configurado
por `LOG_FILE` no `.env`; caminhos relativos sao resolvidos na pasta do executavel.
Antes de validar as raizes DICOM, o worker executa `net use` para cada
compartilhamento UNC configurado. O comando roda na mesma sessao do worker
(LocalSystem, por padrao), e a senha nao e incluida na linha de comando nem no log.
O pacote completo deve ser copiado, incluindo a pasta `_internal`.

### Modo CLI legado (single study, sem RabbitMQ)
```bash
python main.py --study-uid 1.2.840.xxxxx --output-dir output/
```

---

## Como Testar

```bash
# Todos os testes
pytest tests/ -v

# Apenas testes do Worker
pytest tests/test_worker.py -v
```

Para publicar uma mensagem de teste manualmente, use o RabbitMQ Management UI (http://localhost:15672) ou o script de exemplo na secao "Formato das Mensagens" acima.

---

## Como Integrar com o Orchestra

O Orchestra deve:
1. Publicar mensagens no formato `WorkerRequest` (JSON) na fila `worker.dexaocr.request`
2. Escutar respostas na fila `orchestra.worker_results`
3. Correlacionar usando `request_id` e `correlation_id`
4. Tratar `status: error` como falha recuperavel se `retry_count < 3`

---

## Como Criar um Novo Worker a Partir Desta Base

A camada `src/worker/` e generica e reutilizavel:

1. Copie `src/worker/` para o novo projeto
2. Implemente um novo `XxxProcessingService` (equivalente ao `DexaOCRProcessingService`)
3. Configure as variaveis de ambiente com as filas corretas
4. Use `WorkerOrchestrationService` sem modificacao -- apenas injete o novo servico
5. O banco AgileAI ja suporta multiplos workers via coluna `WorkerName`

A especializacao fica 100% isolada no `XxxProcessingService`.
