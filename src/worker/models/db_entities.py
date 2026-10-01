"""
Pydantic models for AgileAI database entities.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WorkerRequestStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"


class WorkerEventStage(str, Enum):
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    VALIDATION_OK = "VALIDATION_OK"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PROCESSING_STARTED = "PROCESSING_STARTED"
    PROCESSING_SUCCESS = "PROCESSING_SUCCESS"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    PUBLISH_STARTED = "PUBLISH_STARTED"
    PUBLISH_SUCCESS = "PUBLISH_SUCCESS"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    ACK_SENT = "ACK_SENT"
    NACK_SENT = "NACK_SENT"


class WorkerEventStatus(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    INFO = "INFO"
    WARNING = "WARNING"


class WorkerRequestRecord(BaseModel):
    """Maps to AgileAI.dbo.WorkerRequests table."""
    id: Optional[int] = None
    request_id: str
    correlation_id: str
    worker_name: str
    study_uid: str
    patient_id: Optional[str] = None
    modality: Optional[str] = None
    exam_type: Optional[str] = None
    status: WorkerRequestStatus = WorkerRequestStatus.RECEIVED
    received_at: Optional[datetime] = None
    processing_started_at: Optional[datetime] = None
    processing_finished_at: Optional[datetime] = None
    processing_time_ms: Optional[int] = None
    output_json: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None
    published_to_rabbitmq: bool = False
    published_at: Optional[datetime] = None
    publish_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WorkerEventRecord(BaseModel):
    """Maps to AgileAI.dbo.WorkerEvents table."""
    id: Optional[int] = None
    request_id: str
    correlation_id: str
    worker_name: str
    stage: WorkerEventStage
    status: WorkerEventStatus
    message: Optional[str] = None
    details: Optional[str] = None
    event_at: Optional[datetime] = None
