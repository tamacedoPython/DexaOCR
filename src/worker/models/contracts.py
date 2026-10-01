"""
Typed Pydantic models for the Orchestra ↔ Worker message contracts.
Schema version: 1.0
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────

class ProcessingStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PROCESSING = "processing"
    RECEIVED = "received"


# ── Request models ──────────────────────────────────────────────────────────────

class WorkerRequestParameters(BaseModel):
    """Optional runtime parameters sent by the orchestrator."""
    language: str = Field(default="por", description="OCR language hint")
    return_debug_data: bool = Field(default=False, description="Include debug metadata in response")

    model_config = {"extra": "allow"}


class WorkerRequestPayload(BaseModel):
    """Clinical/technical payload describing the exam to process."""
    study_uid: str = Field(..., description="DICOM Study UID — mandatory")
    accession_number: Optional[str] = Field(default=None)
    patient_id: Optional[str] = Field(default=None)
    modality: Optional[str] = Field(default="DXA")
    exam_type: Optional[str] = Field(default="Densitometria Ossea")
    inc: Optional[int] = Field(default=None)
    parameters: WorkerRequestParameters = Field(default_factory=WorkerRequestParameters)

    model_config = {"extra": "allow"}


class WorkerRequest(BaseModel):
    """
    Envelope sent by Orchestra to a Worker via RabbitMQ.
    """
    schema_version: str = Field(default="1.0")
    request_id: str = Field(..., description="Unique identifier for this request")
    correlation_id: str = Field(..., description="Correlation ID for tracing across services")
    source_system: str = Field(default="Orchestra")
    target_worker: str = Field(default="DexaOCR")
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    priority: int = Field(default=5, ge=1, le=10)
    retry_count: int = Field(default=0, ge=0)
    callback_queue: str = Field(default="orchestra.worker_results")
    payload: WorkerRequestPayload

    model_config = {"extra": "forbid"}


# ── Response models ─────────────────────────────────────────────────────────────

class WorkerResponseError(BaseModel):
    """Structured error details included in error responses."""
    type: str = Field(..., description="Exception class name or error category")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[str] = Field(default=None, description="Technical details for debugging")


class WorkerResponseMetrics(BaseModel):
    """Processing metrics reported with every response."""
    processing_time_ms: int = Field(..., ge=0)
    pages_processed: Optional[int] = Field(default=None)
    pages_skipped: Optional[int] = Field(default=None)
    ocr_engine: Optional[str] = Field(default=None)


class WorkerResponseOutput(BaseModel):
    """Structured output produced by the DexaOCR worker."""
    exam_type: str = Field(default="DXA")
    structured_data: Dict[str, Any] = Field(default_factory=dict)


class WorkerResponse(BaseModel):
    """
    Envelope published by the Worker to RabbitMQ after processing.
    """
    schema_version: str = Field(default="1.0")
    request_id: str
    correlation_id: str
    worker: str = Field(default="DexaOCR")
    processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: ProcessingStatus
    error: Optional[WorkerResponseError] = Field(default=None)
    metrics: WorkerResponseMetrics
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[WorkerResponseOutput] = Field(default=None)

    model_config = {"extra": "forbid"}

    @classmethod
    def build_success(
        cls,
        request: WorkerRequest,
        structured_data: Dict[str, Any],
        processing_time_ms: int,
        ocr_metadata: Optional[Dict[str, Any]] = None,
    ) -> "WorkerResponse":
        ocr_meta = ocr_metadata or {}
        return cls(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            status=ProcessingStatus.SUCCESS,
            metrics=WorkerResponseMetrics(
                processing_time_ms=processing_time_ms,
                pages_processed=ocr_meta.get("pages_processed"),
                pages_skipped=ocr_meta.get("pages_skipped"),
                ocr_engine=ocr_meta.get("engine"),
            ),
            input={
                "study_uid": request.payload.study_uid,
                "accession_number": request.payload.accession_number,
                "patient_id": request.payload.patient_id,
                "modality": request.payload.modality or "DXA",
                "exam_type": request.payload.exam_type or "DXA",
                "inc": request.payload.inc,
            },
            output=WorkerResponseOutput(
                exam_type=request.payload.exam_type or "DXA",
                structured_data=structured_data,
            ),
        )

    @classmethod
    def build_error(
        cls,
        request_id: str,
        correlation_id: str,
        error_type: str,
        message: str,
        details: Optional[str] = None,
        processing_time_ms: int = 0,
        study_uid: Optional[str] = None,
        request: Optional[WorkerRequest] = None,
    ) -> "WorkerResponse":
        input_payload: Dict[str, Any] = {}
        if request is not None:
            input_payload = {
                "study_uid": request.payload.study_uid,
                "accession_number": request.payload.accession_number,
                "patient_id": request.payload.patient_id,
                "modality": request.payload.modality or "DXA",
                "exam_type": request.payload.exam_type or "DXA",
                "inc": request.payload.inc,
            }
        elif study_uid:
            input_payload = {"study_uid": study_uid}

        return cls(
            request_id=request_id,
            correlation_id=correlation_id,
            status=ProcessingStatus.ERROR,
            error=WorkerResponseError(type=error_type, message=message, details=details),
            metrics=WorkerResponseMetrics(processing_time_ms=processing_time_ms),
            input=input_payload,
            output=None,
        )
