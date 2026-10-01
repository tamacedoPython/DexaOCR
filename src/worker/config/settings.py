"""
Centralized configuration for the DexaOCR Worker.
Reads from environment variables / .env file.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote, urlparse

from dotenv import load_dotenv


def _find_root() -> Path:
    """Project root — works both in normal mode and when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        # PyInstaller onedir: the exe sits in the dist/DexaOCRWorker/ folder
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[4]


_ROOT = _find_root()
load_dotenv(_ROOT / ".env", override=False)


def _split_hosts(raw: str) -> List[str]:
    return [h.strip() for h in raw.split(";") if h.strip()]


def _parse_rabbitmq_url(raw_url: str) -> Optional[dict[str, object]]:
    """Parse an AMQP/AMQPS URL into RabbitMQ connection settings."""
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    if parsed.scheme not in {"amqp", "amqps"}:
        return None

    host = parsed.hostname or None
    port = parsed.port or (5671 if parsed.scheme == "amqps" else 5672)
    user = unquote(parsed.username or "") or None
    password = unquote(parsed.password or "") or None
    path = parsed.path or "/"
    vhost = path if path not in {"", "/"} else "/"

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "vhost": vhost,
    }


class WorkerSettings:
    """All runtime configuration for the DexaOCR Worker service."""

    # ── Worker identity ────────────────────────────────────────────────────────
    worker_name: str
    schema_version: str

    # ── RabbitMQ ───────────────────────────────────────────────────────────────
    rabbitmq_host: str
    rabbitmq_port: int
    rabbitmq_user: str
    rabbitmq_password: str
    rabbitmq_vhost: str
    rabbitmq_queue_input: str
    rabbitmq_queue_output: str
    rabbitmq_prefetch: int
    rabbitmq_heartbeat: int
    rabbitmq_connection_timeout: int
    rabbitmq_retry_delay: float
    rabbitmq_max_retries: int

    # ── Heartbeat ──────────────────────────────────────────────────────────────
    worker_heartbeat_queue: str
    heartbeat_interval: float
    worker_version: str
    worker_service_name: str

    # ── AgileAI / SQL Server ───────────────────────────────────────────────────
    sql_hosts: List[str]
    sql_db: str
    sql_uid: str
    sql_pwd: str
    sql_port: int
    sql_encrypt: str
    sql_trust_cert: str
    odbc_driver: str

    # ── Work directory (temp PNGs per request) ─────────────────────────────────
    work_base_dir: Path

    # ── Logging ────────────────────────────────────────────────────────────────
    log_level: str
    log_file: Optional[Path]

    # ── OCR (passed through to existing dexa_ocr pipeline) ────────────────────
    ocr_engine: str
    tesseract_lang: str
    tesseract_cmd: Optional[str]
    paddleocr_lang: str
    save_debug_images: bool
    dicom_roots: List[str]  # Paths raiz dos DICOMs, tentados em ordem
    dicom_network_username: Optional[str]
    dicom_network_password: Optional[str]
    ocr_max_retries: int    # Extra attempts when L1-L4 or Colo is missing

    def __init__(self) -> None:
        g = os.getenv

        self.worker_name = g("WORKER_NAME", "DexaOCR")
        self.schema_version = g("SCHEMA_VERSION", "1.0")

        # RabbitMQ
        self.rabbitmq_host = g("RABBITMQ_HOST", "localhost")
        self.rabbitmq_port = int(g("RABBITMQ_PORT", "5672"))
        self.rabbitmq_user = g("RABBITMQ_USER", "guest")
        self.rabbitmq_password = g("RABBITMQ_PASSWORD", "guest")
        self.rabbitmq_vhost = g("RABBITMQ_VHOST", "/")

        rabbitmq_url = g("RABBITMQ_URL", "")
        if rabbitmq_url:
            parsed = _parse_rabbitmq_url(rabbitmq_url)
            if parsed:
                self.rabbitmq_host = parsed["host"] or self.rabbitmq_host
                self.rabbitmq_port = int(parsed["port"] or self.rabbitmq_port)
                self.rabbitmq_user = parsed["user"] or self.rabbitmq_user
                self.rabbitmq_password = parsed["password"] or self.rabbitmq_password
                self.rabbitmq_vhost = parsed["vhost"] or self.rabbitmq_vhost

        self.rabbitmq_queue_input = g("RABBITMQ_QUEUE_INPUT", "worker.dexaocr.request")
        self.rabbitmq_queue_output = g("RABBITMQ_QUEUE_OUTPUT", "orchestra.worker_results")
        self.rabbitmq_prefetch = int(g("RABBITMQ_PREFETCH", "1"))
        self.rabbitmq_heartbeat = int(g("RABBITMQ_HEARTBEAT", "60"))
        self.rabbitmq_connection_timeout = int(g("RABBITMQ_CONNECTION_TIMEOUT", "10"))
        self.rabbitmq_retry_delay = float(g("RABBITMQ_RETRY_DELAY", "5.0"))
        self.rabbitmq_max_retries = int(g("RABBITMQ_MAX_RETRIES", "0"))  # 0 = infinite

        # Heartbeat
        self.worker_heartbeat_queue = g("WORKER_HEARTBEAT_QUEUE", "orchestra.worker_heartbeat")
        self.heartbeat_interval = float(g("HEARTBEAT_INTERVAL", "30"))
        self.worker_version = g("WORKER_VERSION", "1.0.0")
        self.worker_service_name = g("WORKER_SERVICE_NAME", "DexaOCRWorker")

        # SQL Server / AgileAI
        raw_hosts = g("SQL_HOSTS", "192.168.1.211;201.48.134.100")
        self.sql_hosts = _split_hosts(raw_hosts)
        self.sql_db = g("SQL_DB", "AgileAI")
        self.sql_uid = g("SQL_UID", "sasys")
        self.sql_pwd = g("SQL_PWD", "")
        self.sql_port = int(g("SQL_PORT", "1433"))
        self.sql_encrypt = g("SQL_ENCRYPT", "no")
        self.sql_trust_cert = g("SQL_TRUST_CERT", "yes")
        self.odbc_driver = g("ODBC_DRIVER", "ODBC Driver 17 for SQL Server")

        # Work directory
        work_raw = Path(g("WORK_BASE_DIR", str(_ROOT / "work")))
        self.work_base_dir = work_raw if work_raw.is_absolute() else _ROOT / work_raw

        # Logging
        self.log_level = g("LOG_LEVEL", "INFO")
        log_file_raw = g("LOG_FILE", "")
        if log_file_raw:
            log_path = Path(log_file_raw)
            self.log_file = log_path if log_path.is_absolute() else _ROOT / log_path
        else:
            self.log_file = None

        # OCR
        self.ocr_engine = g("OCR_ENGINE", "paddleocr")
        self.tesseract_lang = g("TESSERACT_LANG", "por+eng")
        self.tesseract_cmd = g("TESSERACT_CMD") or None
        self.paddleocr_lang = g("PADDLEOCR_LANG", "pt")
        self.save_debug_images = g("SAVE_DEBUG_IMAGES", "false").lower() == "true"
        self.ocr_max_retries = int(g("OCR_MAX_RETRIES", "0"))

        # DICOM Roots
        dicom_roots_raw = g(
            "DICOM_ROOTS",
            r"\\192.168.1.155\dcms_new_ssd_01\DCMs"
            r";\\192.168.1.155\Dados\Database\Dcms"
            r";\\192.168.1.60\dcms",
        )
        self.dicom_roots = [r.strip() for r in dicom_roots_raw.split(";") if r.strip()]
        self.dicom_network_username = g("DICOM_NETWORK_USERNAME") or None
        self.dicom_network_password = g("DICOM_NETWORK_PASSWORD") or None

    @property
    def rabbitmq_url(self) -> str:
        """pika-compatible connection URL."""
        user = quote(self.rabbitmq_user, safe="")
        password = quote(self.rabbitmq_password, safe="")
        return (
            f"amqp://{user}:{password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}{self.rabbitmq_vhost}"
        )


@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    """Return a cached singleton of WorkerSettings."""
    return WorkerSettings()
