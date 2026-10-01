"""
Heartbeat publisher for Orchestra discovery.

Publishes a JSON heartbeat to the `orchestra.worker_heartbeat` queue
(env: WORKER_HEARTBEAT_QUEUE) at a configurable interval so that Orchestra
can discover and monitor this worker instance.

The `instance_id` is stable across restarts: it is written to a small file
on first startup and reused on subsequent starts.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pika
import pika.exceptions

from ..config.settings import WorkerSettings

logger = logging.getLogger("worker.services.heartbeat")

_INSTANCE_ID_FILE = Path(__file__).resolve().parents[4] / ".worker_instance_id"


def _load_or_create_instance_id(worker_name: str) -> str:
    """
    Return a stable instance_id.

    On first run the id is generated from hostname + pid suffix and persisted
    to `.worker_instance_id` in the project root.  Subsequent runs reuse it.
    """
    if _INSTANCE_ID_FILE.exists():
        stored = _INSTANCE_ID_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    hostname = socket.gethostname().upper()
    suffix = str(os.getpid())[-4:]          # last 4 digits of current PID
    instance_id = f"{worker_name.upper()}-{hostname}-{suffix}"
    try:
        _INSTANCE_ID_FILE.write_text(instance_id, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not persist instance_id to %s: %s", _INSTANCE_ID_FILE, exc)
    return instance_id


class HeartbeatService:
    """
    Background thread that emits periodic heartbeat messages to RabbitMQ.

    Each message conforms to the Orchestra heartbeat contract v1.0.

    IMPORTANT: pika.BlockingConnection is NOT thread-safe.  This service owns
    its own private connection and channel that are never shared with the
    consumer or publisher threads.
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Private connection — created inside the heartbeat thread
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[pika.adapters.blocking_connection.BlockingChannel] = None

        self._instance_id = _load_or_create_instance_id(settings.worker_name)
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(
            "Heartbeat service initialised — instance_id=%s queue=%s interval=%.0fs",
            self._instance_id,
            self._settings.worker_heartbeat_queue,
            self._settings.heartbeat_interval,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background heartbeat thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="heartbeat",
            daemon=True,
        )
        self._thread.start()
        logger.info("Heartbeat thread started")

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Heartbeat thread stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_params(self) -> pika.ConnectionParameters:
        return pika.ConnectionParameters(
            host=self._settings.rabbitmq_host,
            port=self._settings.rabbitmq_port,
            virtual_host=self._settings.rabbitmq_vhost,
            credentials=pika.PlainCredentials(
                self._settings.rabbitmq_user,
                self._settings.rabbitmq_password,
            ),
            heartbeat=self._settings.rabbitmq_heartbeat,
            connection_attempts=3,
            retry_delay=2,
            socket_timeout=self._settings.rabbitmq_connection_timeout,
        )

    def _connect(self) -> None:
        """Open a private connection+channel exclusively for this thread."""
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
        except Exception:
            pass
        self._connection = pika.BlockingConnection(self._build_params())
        self._channel = self._connection.channel()
        self._channel.queue_declare(
            queue=self._settings.worker_heartbeat_queue, durable=True
        )
        logger.info(
            "Heartbeat private connection established to %s:%s",
            self._settings.rabbitmq_host,
            self._settings.rabbitmq_port,
        )

    def _ensure_connected(self) -> None:
        try:
            alive = (
                self._connection is not None
                and self._connection.is_open
                and self._channel is not None
                and self._channel.is_open
            )
        except Exception:
            alive = False
        if not alive:
            if self._connection is not None:
                # Was previously connected — genuine reconnect
                logger.warning("Heartbeat connection lost — reconnecting …")
            else:
                # First-time connect — not a warning
                logger.info("Heartbeat: establishing private RabbitMQ connection …")
            self._connect()

    def _build_payload(self) -> bytes:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "schema_version": self._settings.schema_version,
            "worker": self._settings.worker_name,
            "instance_id": self._instance_id,
            "sent_at": now,
            "status": "ONLINE",
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "service_name": self._settings.worker_service_name,
            "version": self._settings.worker_version,
            "started_at": self._started_at,
            "metadata": {
                "queue": self._settings.rabbitmq_queue_input,
            },
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _publish_once(self) -> None:
        self._ensure_connected()
        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
        )
        self._channel.basic_publish(  # type: ignore[union-attr]
            exchange="",
            routing_key=self._settings.worker_heartbeat_queue,
            body=self._build_payload(),
            properties=props,
        )
        logger.debug(
            "Heartbeat published to queue=%s instance=%s",
            self._settings.worker_heartbeat_queue,
            self._instance_id,
        )

    def _loop(self) -> None:
        interval = self._settings.heartbeat_interval
        # Establish private connection inside the thread that will use it
        while not self._stop_event.is_set():
            try:
                self._ensure_connected()
                break
            except Exception as exc:
                logger.warning("Heartbeat: cannot connect (%s) — retrying in %.0fs …", exc, interval)
                self._stop_event.wait(timeout=interval)

        while not self._stop_event.is_set():
            try:
                self._publish_once()
            except Exception as exc:
                logger.warning("Heartbeat publish failed (%s) — will retry next interval", exc)
                # Force reconnect on next iteration
                self._connection = None
                self._channel = None
            self._stop_event.wait(timeout=interval)
