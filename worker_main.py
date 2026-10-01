"""DexaOCR Worker entrypoint for console and Windows Service modes.

Usage:
    DexaOCRWorker.exe             Monitor the log when the service is running;
                                  otherwise run an interactive worker.
    DexaOCRWorker.exe --monitor   Follow the service log without consuming jobs.
    DexaOCRWorker.exe --console   Run an interactive worker (service must be stopped).
    DexaOCRWorker.exe --service   Windows Service mode (used by SCM only).
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.dexa_ocr.utils.logger import (
    ensure_standard_streams,
    get_logger,
    setup_logging,
)

# A process launched by the Windows Service Control Manager may not have
# stdout/stderr. Protect all imports below, including native OCR dependencies
# that can write to those streams during module initialization.
ensure_standard_streams()

from src.worker.config.settings import get_settings
from src.worker.db.connection import WorkerDBConnection
from src.worker.messaging.connection import RabbitMQConnection
from src.worker.messaging.consumer import RabbitMQConsumer
from src.worker.messaging.publisher import RabbitMQPublisher
from src.worker.repositories.worker_repository import WorkerRepository
from src.worker.services.dexa_processing_service import DexaOCRProcessingService
from src.worker.services.dicom_storage_service import (
    connect_dicom_network_shares,
    wait_until_dicom_roots_available,
)
from src.worker.services.heartbeat_service import HeartbeatService
from src.worker.services.worker_service import WorkerOrchestrationService


def _setup_logging(settings) -> None:
    setup_logging(level=settings.log_level, log_file=settings.log_file)


def _build_dependencies(settings):
    """Wire all service dependencies together."""
    db_conn = WorkerDBConnection()
    db_conn.configure(
        hosts=settings.sql_hosts,
        db=settings.sql_db,
        uid=settings.sql_uid,
        pwd=settings.sql_pwd,
        port=settings.sql_port,
        encrypt=settings.sql_encrypt,
        trust_cert=settings.sql_trust_cert,
        driver=settings.odbc_driver,
    )

    rmq_conn = RabbitMQConnection(settings)
    repository = WorkerRepository(db_conn)
    publisher = RabbitMQPublisher(settings, rmq_conn)
    processing_service = DexaOCRProcessingService(settings)
    orchestration_service = WorkerOrchestrationService(
        settings=settings,
        repository=repository,
        publisher=publisher,
        processing_service=processing_service,
    )
    consumer = RabbitMQConsumer(settings, rmq_conn, orchestration_service)
    heartbeat = HeartbeatService(settings)
    return db_conn, rmq_conn, consumer, heartbeat


class WorkerApplication:
    """Owns the worker lifecycle in both console and service modes."""

    def __init__(self) -> None:
        self._stop_requested = threading.Event()
        self._consumer = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        """Request a graceful stop; safe to call from a service-control thread."""
        self._stop_requested.set()
        with self._lock:
            consumer = self._consumer
        if consumer is not None:
            consumer.stop()

    def run(self, register_signals: bool = True) -> int:
        settings = get_settings()
        _setup_logging(settings)
        log = get_logger("worker.main")

        log.info("=" * 60)
        log.info(
            "DexaOCR Worker starting - worker_name=%s schema=%s",
            settings.worker_name,
            settings.schema_version,
        )
        log.info("Input queue  : %s", settings.rabbitmq_queue_input)
        log.info("Output queue : %s", settings.rabbitmq_queue_output)
        log.info("RabbitMQ     : %s:%s", settings.rabbitmq_host, settings.rabbitmq_port)
        log.info("AgileAI DB   : %s on %s", settings.sql_db, settings.sql_hosts)
        log.info("DICOM roots  : %s", "; ".join(settings.dicom_roots))
        log.info("=" * 60)

        settings.work_base_dir.mkdir(parents=True, exist_ok=True)
        db_conn, rmq_conn, consumer, heartbeat = _build_dependencies(settings)
        with self._lock:
            self._consumer = consumer

        if register_signals:
            def _handle_signal(sig, frame):
                log.info("Shutdown signal received - stopping consumer...")
                self.stop()

            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)

        try:
            if self._stop_requested.is_set():
                return 0

            log.info("Waiting for AgileAI DB...")
            try:
                db_conn.wait_until_available(max_wait_seconds=60)
                log.info("AgileAI DB ready")
            except ConnectionError as exc:
                log.error("Cannot connect to AgileAI DB: %s", exc)
                log.warning("Worker will start without DB - events will be logged locally only")

            if self._stop_requested.is_set():
                return 0

            log.info("Connecting to RabbitMQ...")
            rmq_conn.wait_until_available(max_wait_seconds=120)
            log.info("RabbitMQ ready")

            if self._stop_requested.is_set():
                return 0

            log.info("Checking DICOM network storage...")
            prepare_dicom_access = None
            if settings.dicom_network_username or settings.dicom_network_password:
                log.info("DICOM network authentication enabled")
                prepare_dicom_access = lambda: connect_dicom_network_shares(
                    settings.dicom_roots,
                    settings.dicom_network_username or "",
                    settings.dicom_network_password or "",
                )
            wait_until_dicom_roots_available(
                settings.dicom_roots,
                max_wait_seconds=120,
                retry_delay_seconds=5,
                prepare_access=prepare_dicom_access,
                on_retry=lambda exc: log.warning(
                    "DICOM storage not ready; retrying: %s", exc
                ),
            )
            log.info("DICOM network storage ready (%d roots)", len(settings.dicom_roots))

            if self._stop_requested.is_set():
                return 0

            heartbeat.start()
            consumer.start_consuming()
            return 0
        finally:
            log.info("Shutting down...")
            heartbeat.stop()
            rmq_conn.close()
            db_conn.close()
            with self._lock:
                self._consumer = None
            log.info("DexaOCR Worker stopped")


def run_console() -> int:
    """Run directly in the current terminal."""
    return WorkerApplication().run(register_signals=True)


def is_service_running(service_name: str) -> bool | None:
    """Return service state, or ``None`` when Windows cannot be queried."""
    if os.name != "nt":
        return False

    try:
        import pywintypes
        import win32service
        import win32serviceutil
    except ImportError:
        return None

    try:
        state = win32serviceutil.QueryServiceStatus(service_name)[1]
        return state in {
            win32service.SERVICE_START_PENDING,
            win32service.SERVICE_CONTINUE_PENDING,
            win32service.SERVICE_RUNNING,
        }
    except pywintypes.error as exc:
        # ERROR_SERVICE_DOES_NOT_EXIST
        if getattr(exc, "winerror", None) == 1060:
            return False
        return None
    except Exception:
        return None


def run_log_monitor(log_file: Path | None, tail_lines: int = 100) -> int:
    """Display the service log continuously without starting a queue consumer."""
    if log_file is None:
        print("LOG_FILE is not configured; service monitoring is unavailable.")
        return 2

    log_file = log_file.resolve()
    print("=" * 72)
    print("DexaOCR service monitor (read-only; no RabbitMQ consumer is started)")
    print(f"Log: {log_file}")
    print("Press Ctrl+C to close this monitor. The service will keep running.")
    print("=" * 72)

    waiting_message_shown = False
    try:
        while not log_file.exists():
            if not waiting_message_shown:
                print("Waiting for the service log to be created...")
                waiting_message_shown = True
            time.sleep(0.5)

        with log_file.open("r", encoding="utf-8", errors="replace") as stream:
            recent_lines = deque(stream, maxlen=max(0, tail_lines))
            for line in recent_lines:
                print(line, end="")

            while True:
                line = stream.readline()
                if line:
                    print(line, end="")
                    continue

                # Reopen after truncation/replacement (for example after a new
                # deployment) while keeping ordinary appends efficient.
                try:
                    if log_file.stat().st_size < stream.tell():
                        stream.seek(0)
                except OSError:
                    pass
                time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nMonitor closed. DexaOCR service was not stopped.")
        return 0
    except OSError as exc:
        print(f"Unable to read service log: {exc}", file=sys.stderr)
        return 1


def run_windows_service() -> int:
    """Connect this process to the Windows Service Control Manager."""
    if os.name != "nt":
        raise RuntimeError("Windows Service mode is only available on Windows")

    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class DexaOCRWindowsService(win32serviceutil.ServiceFramework):
        _svc_name_ = "DexaOCRWorker"
        _svc_display_name_ = "DexaOCR Worker"
        _svc_description_ = "Processes DXA DICOM studies via RabbitMQ for Orchestra."

        def __init__(self, args):
            super().__init__(args)
            self._application = WorkerApplication()
            self._stopped = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._application.stop()
            win32event.SetEvent(self._stopped)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("DexaOCR Worker service starting")
            try:
                self._application.run(register_signals=False)
            except Exception as exc:
                servicemanager.LogErrorMsg(f"DexaOCR Worker failed: {exc}")
                raise
            finally:
                win32event.SetEvent(self._stopped)
                servicemanager.LogInfoMsg("DexaOCR Worker service stopped")

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(DexaOCRWindowsService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--service" in args:
        return run_windows_service()
    if any(arg in {"-h", "--help"} for arg in args):
        print(__doc__)
        return 0
    unknown = [arg for arg in args if arg not in {"--console", "--monitor"}]
    if unknown:
        print(f"Unknown option: {unknown[0]}", file=sys.stderr)
        print("Use --help to see the available modes.", file=sys.stderr)
        return 2

    if "--console" in args and "--monitor" in args:
        print("Use either --console or --monitor, not both.", file=sys.stderr)
        return 2

    settings = get_settings()
    service_running = is_service_running(settings.worker_service_name)

    if "--monitor" in args:
        return run_log_monitor(settings.log_file)

    if "--console" in args:
        if service_running is not False:
            state = "running" if service_running else "could not be verified"
            print(
                f"DexaOCR service is {state}; interactive worker was not started.",
                file=sys.stderr,
            )
            print("Use --monitor to follow service processing.", file=sys.stderr)
            return 3
        return run_console()

    # A double-click on the packaged executable becomes a safe log viewer when
    # the service is active. Running from source keeps the historical console
    # behavior unless --monitor is explicitly requested.
    if getattr(sys, "frozen", False) and service_running is not False:
        return run_log_monitor(settings.log_file)
    return run_console()


if __name__ == "__main__":
    raise SystemExit(main())
