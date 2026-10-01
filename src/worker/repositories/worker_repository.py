"""
Repository for persisting Worker request lifecycle records in AgileAI.

Provides clean, SQL-free interfaces for the service layer.
All SQL is confined to this module.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db.connection import WorkerDBConnection
from ..models.db_entities import (
    WorkerEventRecord,
    WorkerEventStage,
    WorkerEventStatus,
    WorkerRequestRecord,
    WorkerRequestStatus,
)

logger = logging.getLogger("worker.repository")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerRepository:
    """
    Handles all read/write operations against AgileAI worker tables.

    Tables:  AgileAI.dbo.WorkerRequests
             AgileAI.dbo.WorkerEvents

    Design:  All errors are caught internally; failures are logged but never
             allowed to crash the worker pipeline.  The caller receives False
             on failure, so it can decide whether to escalate.
    """

    def __init__(self, db: WorkerDBConnection) -> None:
        self._db = db

    # ── WorkerRequests ─────────────────────────────────────────────────────────

    def insert_request(self, record: WorkerRequestRecord) -> int:
        """
        Insert a new request row (idempotente).

        Returns:
            1  — inserted successfully (new request)
            0  — row already existed (redelivery detected)
            -1 — database error
        """
        # Usa INSERT … WHERE NOT EXISTS para ser idempotente em caso de reentrega
        # (AMQP garante at-least-once delivery; o mesmo RequestID pode chegar novamente
        # se a conexão caiu após o processamento mas antes do ACK).
        sql = """
            INSERT INTO AgileAI.dbo.WorkerRequests
                (RequestID, CorrelationID, WorkerName, StudyUID, PatientID,
                 Modality, ExamType, Status, ReceivedAt, CreatedAt, UpdatedAt)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM AgileAI.dbo.WorkerRequests WHERE RequestID = ?
            )
        """
        now = _now()
        try:
            cur = self._db.cursor()
            cur.execute(
                sql,
                record.request_id,
                record.correlation_id,
                record.worker_name,
                record.study_uid,
                record.patient_id,
                record.modality,
                record.exam_type,
                record.status.value,
                record.received_at or now,
                now,
                now,
                record.request_id,  # parâmetro do WHERE NOT EXISTS
            )
            affected = cur.rowcount
            self._db.commit()
            if affected == 0:
                logger.warning(
                    "WorkerRequest %s já existe — reentrega detectada, ignorando INSERT.",
                    record.request_id,
                )
            else:
                logger.debug("Inserted WorkerRequest row for %s", record.request_id)
            return affected
        except Exception as exc:
            self._db.rollback()
            logger.error("Failed to insert WorkerRequest %s: %s", record.request_id, exc)
            return -1

    def get_stored_output(self, request_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the stored OCR result for a request that already completed.

        Returns a dict with ``output_json`` (dict) and ``processing_time_ms`` (int),
        or ``None`` if the request has no stored output yet (not yet processed or failed
        before saving the result).
        """
        sql = """
            SELECT OutputJson, ProcessingTimeMs
            FROM AgileAI.dbo.WorkerRequests
            WHERE RequestID = ? AND OutputJson IS NOT NULL
        """
        try:
            cur = self._db.cursor()
            cur.execute(sql, request_id)
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "output_json": json.loads(row[0]),
                "processing_time_ms": row[1] or 0,
            }
        except Exception as exc:
            logger.error("get_stored_output failed for %s: %s", request_id, exc)
            return None

    def mark_processing_started(self, request_id: str) -> bool:
        sql = """
            UPDATE AgileAI.dbo.WorkerRequests
            SET Status = ?, ProcessingStartedAt = ?, UpdatedAt = ?
            WHERE RequestID = ?
        """
        now = _now()
        try:
            self._db.cursor().execute(
                sql,
                WorkerRequestStatus.PROCESSING.value,
                now,
                now,
                request_id,
            )
            self._db.commit()
            return True
        except Exception as exc:
            self._db.rollback()
            logger.error("mark_processing_started failed for %s: %s", request_id, exc)
            return False

    def mark_processing_success(
        self,
        request_id: str,
        processing_time_ms: int,
        output_json: dict,
    ) -> bool:
        sql = """
            UPDATE AgileAI.dbo.WorkerRequests
            SET Status = ?, ProcessingFinishedAt = ?, ProcessingTimeMs = ?,
                OutputJson = ?, UpdatedAt = ?
            WHERE RequestID = ?
        """
        now = _now()
        try:
            self._db.cursor().execute(
                sql,
                WorkerRequestStatus.SUCCESS.value,
                now,
                processing_time_ms,
                json.dumps(output_json, ensure_ascii=False),
                now,
                request_id,
            )
            self._db.commit()
            return True
        except Exception as exc:
            self._db.rollback()
            logger.error("mark_processing_success failed for %s: %s", request_id, exc)
            return False

    def mark_processing_error(
        self,
        request_id: str,
        processing_time_ms: int,
        error_type: str,
        error_message: str,
    ) -> bool:
        sql = """
            UPDATE AgileAI.dbo.WorkerRequests
            SET Status = ?, ProcessingFinishedAt = ?, ProcessingTimeMs = ?,
                ErrorType = ?, ErrorMessage = ?, UpdatedAt = ?
            WHERE RequestID = ?
        """
        now = _now()
        try:
            self._db.cursor().execute(
                sql,
                WorkerRequestStatus.ERROR.value,
                now,
                processing_time_ms,
                error_type,
                error_message[:2000],  # guard column width
                now,
                request_id,
            )
            self._db.commit()
            return True
        except Exception as exc:
            self._db.rollback()
            logger.error("mark_processing_error failed for %s: %s", request_id, exc)
            return False

    def mark_published(self, request_id: str) -> bool:
        sql = """
            UPDATE AgileAI.dbo.WorkerRequests
            SET Status = ?, PublishedToRabbitMQ = 1, PublishedAt = ?, UpdatedAt = ?
            WHERE RequestID = ?
        """
        now = _now()
        try:
            self._db.cursor().execute(
                sql,
                WorkerRequestStatus.PUBLISHED.value,
                now,
                now,
                request_id,
            )
            self._db.commit()
            return True
        except Exception as exc:
            self._db.rollback()
            logger.error("mark_published failed for %s: %s", request_id, exc)
            return False

    def mark_publish_failed(self, request_id: str, error: str) -> bool:
        sql = """
            UPDATE AgileAI.dbo.WorkerRequests
            SET Status = ?, PublishedToRabbitMQ = 0, PublishError = ?, UpdatedAt = ?
            WHERE RequestID = ?
        """
        now = _now()
        try:
            self._db.cursor().execute(
                sql,
                WorkerRequestStatus.PUBLISH_FAILED.value,
                error[:1000],
                now,
                request_id,
            )
            self._db.commit()
            return True
        except Exception as exc:
            self._db.rollback()
            logger.error("mark_publish_failed failed for %s: %s", request_id, exc)
            return False

    # ── WorkerEvents ───────────────────────────────────────────────────────────

    def log_event(
        self,
        request_id: str,
        correlation_id: str,
        worker_name: str,
        stage: WorkerEventStage,
        status: WorkerEventStatus,
        message: Optional[str] = None,
        details: Optional[str] = None,
    ) -> bool:
        """Append a single event row to the audit trail."""
        sql = """
            INSERT INTO AgileAI.dbo.WorkerEvents
                (RequestID, CorrelationID, WorkerName, Stage, Status,
                 Message, Details, EventAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            self._db.cursor().execute(
                sql,
                request_id,
                correlation_id,
                worker_name,
                stage.value,
                status.value,
                message[:500] if message else None,
                details[:2000] if details else None,
                _now(),
            )
            self._db.commit()
            return True
        except Exception as exc:
            self._db.rollback()
            logger.error("log_event failed for %s / %s: %s", request_id, stage, exc)
            return False
