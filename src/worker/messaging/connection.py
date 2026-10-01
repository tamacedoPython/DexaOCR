"""
RabbitMQ connection manager with automatic reconnection.
Shared by consumer and publisher.
"""
from __future__ import annotations

import logging
import time
import threading
from typing import Optional

import pika
import pika.exceptions

from ..config.settings import WorkerSettings

logger = logging.getLogger("worker.messaging.connection")


class RabbitMQConnection:
    """
    Thread-safe, self-healing RabbitMQ connection.

    Provides a single persistent connection and channel. On any
    failure the connection is recreated transparently.

    Usage:
        conn = RabbitMQConnection(settings)
        channel = conn.channel()
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[pika.adapters.blocking_connection.BlockingChannel] = None
        self._lock = threading.Lock()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _build_parameters(self) -> pika.ConnectionParameters:
        credentials = pika.PlainCredentials(
            self._settings.rabbitmq_user,
            self._settings.rabbitmq_password,
        )
        return pika.ConnectionParameters(
            host=self._settings.rabbitmq_host,
            port=self._settings.rabbitmq_port,
            virtual_host=self._settings.rabbitmq_vhost,
            credentials=credentials,
            heartbeat=self._settings.rabbitmq_heartbeat,
            connection_attempts=3,
            retry_delay=2,
            socket_timeout=self._settings.rabbitmq_connection_timeout,
        )

    def _is_alive(self) -> bool:
        try:
            return (
                self._connection is not None
                and self._connection.is_open
                and self._channel is not None
                and self._channel.is_open
            )
        except Exception:
            return False

    def _do_connect(self) -> None:
        params = self._build_parameters()
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()
        self._channel.basic_qos(prefetch_count=self._settings.rabbitmq_prefetch)
        logger.info(
            "RabbitMQ connected to %s:%s",
            self._settings.rabbitmq_host,
            self._settings.rabbitmq_port,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Establish connection. Blocks until successful or raises."""
        with self._lock:
            self._do_connect()

    def ensure_connected(self) -> None:
        """Reconnect if connection is stale."""
        with self._lock:
            if not self._is_alive():
                logger.warning("RabbitMQ connection lost — reconnecting …")
                self._do_connect()

    def channel(self) -> pika.adapters.blocking_connection.BlockingChannel:
        self.ensure_connected()
        return self._channel  # type: ignore[return-value]

    def close(self) -> None:
        with self._lock:
            try:
                if self._connection and self._connection.is_open:
                    self._connection.close()
                    logger.info("RabbitMQ connection closed")
            except Exception as exc:
                logger.warning("Error closing RabbitMQ connection: %s", exc)
            finally:
                self._connection = None
                self._channel = None

    def request_stop_consuming(self) -> None:
        """Ask Pika's I/O thread to leave ``start_consuming`` safely."""
        with self._lock:
            connection = self._connection
            channel = self._channel

        try:
            if (
                connection is not None
                and connection.is_open
                and channel is not None
                and channel.is_open
            ):
                connection.add_callback_threadsafe(channel.stop_consuming)
        except Exception as exc:
            logger.debug("Could not schedule consumer stop: %s", exc)

    def wait_until_available(self, max_wait_seconds: int = 120) -> None:
        """Block until RabbitMQ accepts a connection."""
        deadline = time.monotonic() + max_wait_seconds
        delay = self._settings.rabbitmq_retry_delay
        while True:
            try:
                self.connect()
                return
            except Exception as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectionError(
                        f"RabbitMQ unavailable after {max_wait_seconds}s: {exc}"
                    ) from exc
                logger.warning(
                    "RabbitMQ not available (%s) — retrying in %.0fs …", exc, delay
                )
                time.sleep(min(delay, remaining))
