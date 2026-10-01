"""
Worker Orchestration Service — the internal coordinator of the DexaOCR Worker.

This service is the single point of control for each message cycle:
  1. Register reception in AgileAI
  2. Call DexaOCRProcessingService
  3. Register result in AgileAI
  4. Publish response to RabbitMQ
  5. Register publication status in AgileAI

The RabbitMQ consumer calls handle_message() and only ACKs after this method
returns successfully (or NACKs if it raises).

Design: no OCR logic, no SQL, no RabbitMQ protocol here — only orchestration.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

from ..config.settings import WorkerSettings
from ..messaging.publisher import RabbitMQPublisher
from ..models.contracts import WorkerRequest, WorkerResponse
from ..models.db_entities import (
    WorkerEventStage,
    WorkerEventStatus,
    WorkerRequestRecord,
    WorkerRequestStatus,
)
from ..repositories.worker_repository import WorkerRepository
from .dexa_processing_service import DexaOCRProcessingService, DexaProcessingError

logger = logging.getLogger("worker.services.orchestration")


class WorkerOrchestrationService:
    """
    Orchestrates the complete lifecycle of a single Worker request.

    Dependencies are injected — this class is testable with mocks.
    """

    def __init__(
        self,
        settings: WorkerSettings,
        repository: WorkerRepository,
        publisher: RabbitMQPublisher,
        processing_service: DexaOCRProcessingService,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._publisher = publisher
        self._processing = processing_service

    # ── Public entry point ────────────────────────────────────────────────────

    def handle_message(self, request: WorkerRequest, raw_message: Dict[str, Any]) -> None:
        """
        Full request lifecycle.  Called by RabbitMQConsumer for each message.

        Raises:
            Exception: propagated to consumer so it can NACK appropriately.
        """
        req_id = request.request_id
        corr_id = request.correlation_id
        study_uid = request.payload.study_uid
        worker_name = self._settings.worker_name

        logger.info(
            "handle_message START — request_id=%s correlation_id=%s study_uid=%s",
            req_id, corr_id, study_uid,
        )

        # ── Step 1: Persist reception ─────────────────────────────────────────
        record = WorkerRequestRecord(
            request_id=req_id,
            correlation_id=corr_id,
            worker_name=worker_name,
            study_uid=study_uid,
            patient_id=request.payload.patient_id,
            modality=request.payload.modality,
            exam_type=request.payload.exam_type,
            status=WorkerRequestStatus.RECEIVED,
            received_at=datetime.now(timezone.utc),
        )
        is_new = self._repo.insert_request(record)
        self._repo.log_event(
            req_id, corr_id, worker_name,
            WorkerEventStage.MESSAGE_RECEIVED, WorkerEventStatus.OK,
            message=f"Request received for study_uid={study_uid}",
        )

        # ── Redelivery shortcut ───────────────────────────────────────────────
        # insert_request returns 0 when the row already exists (redelivery after
        # a TCP reset during publish/ACK).  If the OCR result is already stored,
        # skip re-running the pipeline and jump straight to re-publish.
        if is_new == 0:
            logger.warning(
                "Redelivery detected for request_id=%s — checking for stored result", req_id
            )
            stored = self._repo.get_stored_output(req_id)
            if stored and stored.get("output_json"):
                logger.info(
                    "Stored result found for request_id=%s — skipping OCR and re-publishing",
                    req_id,
                )
                response = WorkerResponse.build_success(
                    request=request,
                    structured_data=stored["output_json"],
                    processing_time_ms=stored["processing_time_ms"],
                    ocr_metadata={"engine": "redelivery_replay"},
                )
                self._do_publish(req_id, corr_id, worker_name, request, response)
                return
            logger.warning(
                "No stored result for request_id=%s — re-running OCR (first run may have "
                "failed before saving)",
                req_id,
            )

        # ── Step 2: Processing ────────────────────────────────────────────────
        self._repo.mark_processing_started(req_id)
        self._repo.log_event(
            req_id, corr_id, worker_name,
            WorkerEventStage.PROCESSING_STARTED, WorkerEventStatus.INFO,
        )

        start_ts = time.monotonic()
        response: WorkerResponse

        try:
            structured_data, ocr_metadata = self._processing.process(
                study_uid=study_uid,
                request_id=req_id,
                return_debug_data=request.payload.parameters.return_debug_data,
            )

            processing_time_ms = int((time.monotonic() - start_ts) * 1000)

            # ── Step 3: Persist success ───────────────────────────────────────
            self._repo.mark_processing_success(req_id, processing_time_ms, structured_data)
            self._repo.log_event(
                req_id, corr_id, worker_name,
                WorkerEventStage.PROCESSING_SUCCESS, WorkerEventStatus.OK,
                message=f"Processed in {processing_time_ms}ms",
                details=f"engine={ocr_metadata.get('engine')} pages={ocr_metadata.get('pages_processed')}",
            )

            response = WorkerResponse.build_success(
                request=request,
                structured_data=structured_data,
                processing_time_ms=processing_time_ms,
                ocr_metadata=ocr_metadata,
            )

        except DexaProcessingError as exc:
            processing_time_ms = int((time.monotonic() - start_ts) * 1000)
            error_msg = str(exc)
            logger.error("Processing failed for request_id=%s: %s", req_id, error_msg)

            self._repo.mark_processing_error(
                req_id, processing_time_ms, type(exc).__name__, error_msg
            )
            self._repo.log_event(
                req_id, corr_id, worker_name,
                WorkerEventStage.PROCESSING_FAILED, WorkerEventStatus.FAILED,
                message="DexaOCR processing failed",
                details=error_msg[:2000],
            )

            response = WorkerResponse.build_error(
                request_id=req_id,
                correlation_id=corr_id,
                error_type=type(exc).__name__,
                message="Falha ao processar exame DXA",
                details=error_msg,
                processing_time_ms=processing_time_ms,
                study_uid=study_uid,
            )

        except Exception as exc:
            processing_time_ms = int((time.monotonic() - start_ts) * 1000)
            error_msg = str(exc)
            logger.error(
                "Unexpected error for request_id=%s: %s", req_id, error_msg, exc_info=True
            )

            self._repo.mark_processing_error(
                req_id, processing_time_ms, type(exc).__name__, error_msg
            )
            self._repo.log_event(
                req_id, corr_id, worker_name,
                WorkerEventStage.PROCESSING_FAILED, WorkerEventStatus.FAILED,
                message="Unexpected error during processing",
                details=error_msg[:2000],
            )

            response = WorkerResponse.build_error(
                request_id=req_id,
                correlation_id=corr_id,
                error_type=type(exc).__name__,
                message="Erro inesperado no processamento",
                details=error_msg,
                processing_time_ms=processing_time_ms,
                study_uid=study_uid,
            )

        # ── Step 4: Publish response ──────────────────────────────────────────
        self._do_publish(req_id, corr_id, worker_name, request, response)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _do_publish(
        self,
        req_id: str,
        corr_id: str,
        worker_name: str,
        request: WorkerRequest,
        response: WorkerResponse,
    ) -> None:
        """Publish *response* to RabbitMQ and update the DB publish status."""
        self._repo.log_event(
            req_id, corr_id, worker_name,
            WorkerEventStage.PUBLISH_STARTED, WorkerEventStatus.INFO,
            message=f"Publishing to {request.callback_queue}",
        )

        try:
            self._publisher.publish(response, queue=request.callback_queue)
            self._repo.mark_published(req_id)
            self._repo.log_event(
                req_id, corr_id, worker_name,
                WorkerEventStage.PUBLISH_SUCCESS, WorkerEventStatus.OK,
                message=f"Response published to {request.callback_queue}",
            )
            logger.info(
                "handle_message COMPLETE — request_id=%s status=%s",
                req_id, response.status,
            )

        except Exception as exc:
            publish_err = str(exc)
            logger.error(
                "Failed to publish response for request_id=%s: %s", req_id, publish_err
            )
            self._repo.mark_publish_failed(req_id, publish_err)
            self._repo.log_event(
                req_id, corr_id, worker_name,
                WorkerEventStage.PUBLISH_FAILED, WorkerEventStatus.FAILED,
                message="Failed to publish response to RabbitMQ",
                details=publish_err[:2000],
            )
            # NÃO re-raise: o OCR já concluiu e o resultado está persistido no banco.
            # Re-enfileirar causaria re-processamento desnecessário e violação de UNIQUE
            # na tabela WorkerRequests. O ACK será enviado pelo consumer normalmente.
            logger.warning(
                "Publish falhou para request_id=%s — ACK será enviado assim mesmo "
                "(resultado salvo no banco). Orquestrador pode republicar via mark_publish_failed.",
                req_id,
            )
