"""
Unit tests for the Worker layer.

All external dependencies (DB, RabbitMQ, DexaOCR pipeline) are mocked.
Tests verify:
  - WorkerRequest / WorkerResponse model validation and serialization
  - WorkerOrchestrationService flow (success and error paths)
  - WorkerRepository method calls (via mock DB)
  - RabbitMQPublisher publish call
  - DexaOCRProcessingService isolation
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.worker.models.contracts import (
    ProcessingStatus,
    WorkerRequest,
    WorkerRequestParameters,
    WorkerRequestPayload,
    WorkerResponse,
    WorkerResponseError,
    WorkerResponseMetrics,
)
from src.worker.models.db_entities import (
    WorkerEventStage,
    WorkerEventStatus,
    WorkerRequestStatus,
)
from src.worker.services.worker_service import WorkerOrchestrationService
from src.worker.services.dexa_processing_service import DexaProcessingError


# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_REQUEST_DICT: Dict[str, Any] = {
    "schema_version": "1.0",
    "request_id": "req-001",
    "correlation_id": "corr-001",
    "source_system": "Orchestra",
    "target_worker": "DexaOCR",
    "requested_at": "2026-04-23T10:00:00Z",
    "priority": 5,
    "retry_count": 0,
    "callback_queue": "orchestra.worker_results",
    "payload": {
        "study_uid": "1.2.840.99999",
        "accession_number": "ACC-001",
        "patient_id": "PAT-001",
        "modality": "DXA",
        "exam_type": "Densitometria Ossea",
        "inc": 4061107,
        "parameters": {
            "language": "por",
            "return_debug_data": False,
        },
    },
}


@pytest.fixture
def valid_request() -> WorkerRequest:
    return WorkerRequest.model_validate(SAMPLE_REQUEST_DICT)


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.worker_name = "DexaOCR"
    s.schema_version = "1.0"
    s.rabbitmq_queue_input = "worker.dexaocr.request"
    s.rabbitmq_queue_output = "orchestra.worker_results"
    return s


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.insert_request.return_value = 1  # 1 = new request
    repo.get_stored_output.return_value = None
    repo.mark_processing_started.return_value = True
    repo.mark_processing_success.return_value = True
    repo.mark_processing_error.return_value = True
    repo.mark_published.return_value = True
    repo.mark_publish_failed.return_value = True
    repo.log_event.return_value = True
    return repo


@pytest.fixture
def mock_publisher():
    return MagicMock()


@pytest.fixture
def mock_processing_service():
    svc = MagicMock()
    svc.process.return_value = (
        {
            "patient_info": {"name": "JOAO SILVA"},
            "lumbar_spine": {},
            "right_femur": {},
        },
        {
            "engine": "tesseract",
            "pages_processed": 2,
            "pages_skipped": 1,
        },
    )
    return svc


@pytest.fixture
def orchestration_service(mock_settings, mock_repository, mock_publisher, mock_processing_service):
    return WorkerOrchestrationService(
        settings=mock_settings,
        repository=mock_repository,
        publisher=mock_publisher,
        processing_service=mock_processing_service,
    )


# ── Model tests ────────────────────────────────────────────────────────────────

class TestWorkerRequestModel:
    def test_valid_request_parses(self):
        req = WorkerRequest.model_validate(SAMPLE_REQUEST_DICT)
        assert req.request_id == "req-001"
        assert req.payload.study_uid == "1.2.840.99999"

    def test_missing_study_uid_fails(self):
        bad = {**SAMPLE_REQUEST_DICT, "payload": {"accession_number": "ACC"}}
        with pytest.raises(ValidationError):
            WorkerRequest.model_validate(bad)

    def test_missing_request_id_fails(self):
        bad = {k: v for k, v in SAMPLE_REQUEST_DICT.items() if k != "request_id"}
        with pytest.raises(ValidationError):
            WorkerRequest.model_validate(bad)

    def test_extra_fields_rejected(self):
        bad = {**SAMPLE_REQUEST_DICT, "unknown_field": "should_fail"}
        with pytest.raises(ValidationError):
            WorkerRequest.model_validate(bad)

    def test_default_priority(self):
        minimal = {
            "request_id": "r1",
            "correlation_id": "c1",
            "payload": {"study_uid": "1.2.3"},
        }
        req = WorkerRequest.model_validate(minimal)
        assert req.priority == 5

    def test_serialization_roundtrip(self):
        req = WorkerRequest.model_validate(SAMPLE_REQUEST_DICT)
        json_str = req.model_dump_json()
        restored = WorkerRequest.model_validate_json(json_str)
        assert restored.request_id == req.request_id
        assert restored.payload.study_uid == req.payload.study_uid


class TestWorkerResponseModel:
    def test_success_factory(self, valid_request):
        req = valid_request
        resp = WorkerResponse.build_success(
            request=req,
            structured_data={"lumbar_spine": {}},
            processing_time_ms=1234,
            ocr_metadata={"engine": "paddleocr", "pages_processed": 2},
        )
        assert resp.status == ProcessingStatus.SUCCESS
        assert resp.request_id == req.request_id
        assert resp.error is None
        assert resp.output is not None
        assert resp.output.structured_data == {"lumbar_spine": {}}
        assert resp.metrics.processing_time_ms == 1234
        assert resp.metrics.ocr_engine == "paddleocr"

    def test_error_factory(self):
        resp = WorkerResponse.build_error(
            request_id="req-001",
            correlation_id="corr-001",
            error_type="DexaProcessingError",
            message="Pipeline failed",
            details="No DICOM found",
            processing_time_ms=500,
            study_uid="1.2.3",
        )
        assert resp.status == ProcessingStatus.ERROR
        assert resp.output is None
        assert resp.error is not None
        assert resp.error.type == "DexaProcessingError"
        assert resp.error.message == "Pipeline failed"

    def test_response_json_serializable(self, valid_request):
        resp = WorkerResponse.build_success(
            request=valid_request,
            structured_data={"patient_info": {}},
            processing_time_ms=100,
        )
        payload = json.loads(resp.model_dump_json())
        assert payload["status"] == "success"
        assert payload["worker"] == "DexaOCR"
        assert payload["input"]["study_uid"] == valid_request.payload.study_uid
        assert payload["input"]["accession_number"] == valid_request.payload.accession_number
        assert payload["input"]["patient_id"] == valid_request.payload.patient_id
        assert payload["input"]["modality"] == valid_request.payload.modality
        assert payload["input"]["exam_type"] == valid_request.payload.exam_type
        assert payload["input"]["inc"] == valid_request.payload.inc


# ── Orchestration service tests ────────────────────────────────────────────────

class TestWorkerOrchestrationService:
    def test_successful_flow(self, orchestration_service, valid_request, mock_repository, mock_publisher):
        orchestration_service.handle_message(valid_request, {})

        # Repository interactions
        mock_repository.insert_request.assert_called_once()
        mock_repository.mark_processing_started.assert_called_once_with("req-001")
        mock_repository.mark_processing_success.assert_called_once()
        mock_repository.mark_published.assert_called_once_with("req-001")

        # Publisher called once
        mock_publisher.publish.assert_called_once()
        published_response: WorkerResponse = mock_publisher.publish.call_args[0][0]
        assert published_response.status == ProcessingStatus.SUCCESS
        assert published_response.error is None

    def test_processing_failure_publishes_error_response(
        self, orchestration_service, valid_request, mock_processing_service, mock_publisher, mock_repository
    ):
        mock_processing_service.process.side_effect = DexaProcessingError("No DICOM found")

        orchestration_service.handle_message(valid_request, {})

        mock_repository.mark_processing_error.assert_called_once()
        mock_repository.mark_published.assert_called_once()

        published: WorkerResponse = mock_publisher.publish.call_args[0][0]
        assert published.status == ProcessingStatus.ERROR
        assert published.error is not None

    def test_redelivery_skips_ocr_and_republishes(
        self, orchestration_service, valid_request, mock_repository,
        mock_processing_service, mock_publisher
    ):
        """Reentrega com resultado salvo no banco deve pular OCR e re-publicar."""
        stored_data = {
            "patient_info": {"name": "JOAO SILVA"},
            "lumbar_spine": {"rows": []},
        }
        mock_repository.insert_request.return_value = 0  # redelivery
        mock_repository.get_stored_output.return_value = {
            "output_json": stored_data,
            "processing_time_ms": 4200,
        }

        orchestration_service.handle_message(valid_request, {})

        # OCR must NOT run
        mock_processing_service.process.assert_not_called()

        # Publisher must be called with the stored result
        mock_publisher.publish.assert_called_once()
        published: WorkerResponse = mock_publisher.publish.call_args[0][0]
        assert published.status == ProcessingStatus.SUCCESS
        assert published.output is not None
        assert published.output.structured_data == stored_data
        assert published.metrics.processing_time_ms == 4200
        assert published.metrics.ocr_engine == "redelivery_replay"

    def test_redelivery_without_stored_result_runs_ocr(
        self, orchestration_service, valid_request, mock_repository,
        mock_processing_service
    ):
        """Reentrega sem resultado salvo (primeira execução falhou antes de salvar) deve rodar OCR."""
        mock_repository.insert_request.return_value = 0  # redelivery
        mock_repository.get_stored_output.return_value = None  # no stored result

        orchestration_service.handle_message(valid_request, {})

        mock_processing_service.process.assert_called_once()

    def test_publish_failure_does_not_raise(
        self, orchestration_service, valid_request, mock_publisher, mock_repository
    ):
        """Falha de publish NÃO deve re-raise: processamento concluiu, resultado no banco.
        O consumer envia ACK normalmente; re-raise causaria NACK + reentrega + duplicate key.
        """
        mock_publisher.publish.side_effect = RuntimeError("RabbitMQ down")

        # Não deve lançar exceção
        orchestration_service.handle_message(valid_request, {})

        # Deve registrar a falha de publish no repositório
        mock_repository.mark_publish_failed.assert_called_once()

        # Deve ter logado evento PUBLISH_FAILED
        stages_logged = [
            call.args[3]
            for call in mock_repository.log_event.call_args_list
        ]
        assert WorkerEventStage.PUBLISH_FAILED in stages_logged

    def test_log_events_called_in_success(self, orchestration_service, valid_request, mock_repository):
        orchestration_service.handle_message(valid_request, {})

        stages_logged = [
            call.args[3]
            for call in mock_repository.log_event.call_args_list
        ]
        assert WorkerEventStage.MESSAGE_RECEIVED in stages_logged
        assert WorkerEventStage.PROCESSING_STARTED in stages_logged
        assert WorkerEventStage.PROCESSING_SUCCESS in stages_logged
        assert WorkerEventStage.PUBLISH_SUCCESS in stages_logged

    def test_processing_service_receives_correct_study_uid(
        self, orchestration_service, valid_request, mock_processing_service
    ):
        orchestration_service.handle_message(valid_request, {})
        mock_processing_service.process.assert_called_once_with(
            study_uid="1.2.840.99999",
            request_id="req-001",
            return_debug_data=False,
        )


# ── Repository tests (DB interaction via mock) ─────────────────────────────────

class TestWorkerRepository:
    """Test that WorkerRepository constructs correct SQL calls via mock cursor."""

    def _make_repo(self):
        from src.worker.repositories.worker_repository import WorkerRepository
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1  # default: new row inserted
        mock_db.cursor.return_value = mock_cursor
        return WorkerRepository(mock_db), mock_db, mock_cursor

    def test_insert_request_commits(self):
        from src.worker.models.db_entities import WorkerRequestRecord
        repo, mock_db, mock_cursor = self._make_repo()

        record = WorkerRequestRecord(
            request_id="r1",
            correlation_id="c1",
            worker_name="DexaOCR",
            study_uid="1.2.3",
        )
        result = repo.insert_request(record)
        assert result == 1
        mock_cursor.execute.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_insert_request_returns_minus_one_on_db_error(self):
        from src.worker.models.db_entities import WorkerRequestRecord
        repo, mock_db, mock_cursor = self._make_repo()
        mock_cursor.execute.side_effect = Exception("DB error")

        record = WorkerRequestRecord(
            request_id="r1",
            correlation_id="c1",
            worker_name="DexaOCR",
            study_uid="1.2.3",
        )
        result = repo.insert_request(record)
        assert result == -1

    def test_log_event_commits(self):
        repo, mock_db, mock_cursor = self._make_repo()
        result = repo.log_event(
            "r1", "c1", "DexaOCR",
            WorkerEventStage.MESSAGE_RECEIVED,
            WorkerEventStatus.OK,
            message="test",
        )
        assert result is True
        mock_db.commit.assert_called()
