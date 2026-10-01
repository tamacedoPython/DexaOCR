"""
RabbitMQ consumer.

Continuously listens on the input queue, deserializes messages,
delegates processing to WorkerOrchestrationService, then ACKs or NACKs.

Design decisions:
- ACK is sent only AFTER the full cycle (process → persist → publish).
- NACK with requeue=False is sent for invalid messages (poison pill prevention).
- On transient errors, NACK with requeue=True allows retry (retry_count tracked in message).
- The consumer loop is resilient to connection drops — it reconnects and resumes.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Callable

import pika
import pika.exceptions

from ..config.settings import WorkerSettings
from ..models.contracts import WorkerRequest
from .connection import RabbitMQConnection

if TYPE_CHECKING:
    from ..services.worker_service import WorkerOrchestrationService

logger = logging.getLogger("worker.messaging.consumer")

MessageHandler = Callable[[WorkerRequest, dict], None]


class RabbitMQConsumer:
    """
    Blocking consumer for the DexaOCR worker input queue.

    The consumer calls `service.handle_message()` for each message received.
    All ACK/NACK decisions are made here; the service must raise on failure.
    """

    def __init__(
        self,
        settings: WorkerSettings,
        conn: RabbitMQConnection,
        service: "WorkerOrchestrationService",
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._service = service
        self._running = False

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _declare_queues(self) -> None:
        channel = self._conn.channel()
        # Input queue
        channel.queue_declare(
            queue=self._settings.rabbitmq_queue_input,
            durable=True,
            arguments={
                # Dead-letter exchange: unroutable/expired messages go here
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": f"{self._settings.rabbitmq_queue_input}.dlq",
            },
        )
        # Dead-letter queue
        channel.queue_declare(
            queue=f"{self._settings.rabbitmq_queue_input}.dlq",
            durable=True,
        )
        # Output queue (declare here for consistency)
        channel.queue_declare(queue=self._settings.rabbitmq_queue_output, durable=True)
        logger.info(
            "Queues declared: input=%s output=%s",
            self._settings.rabbitmq_queue_input,
            self._settings.rabbitmq_queue_output,
        )

    # ── Message callback ──────────────────────────────────────────────────────

    def _on_message(
        self,
        channel: pika.adapters.blocking_connection.BlockingChannel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,
        body: bytes,
    ) -> None:
        delivery_tag = method.delivery_tag
        logger.info(
            "Message received delivery_tag=%s routing_key=%s",
            delivery_tag,
            method.routing_key,
        )

        # ── Step 1: Deserialize ───────────────────────────────────────────────
        try:
            raw = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Malformed message body (not JSON): %s", exc)
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
            return

        # ── Step 2: Validate contract ─────────────────────────────────────────
        try:
            request = WorkerRequest.model_validate(raw)
        except Exception as exc:
            logger.error("Invalid WorkerRequest schema: %s", exc)
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
            return

        logger.info(
            "Processing request_id=%s correlation_id=%s study_uid=%s",
            request.request_id,
            request.correlation_id,
            request.payload.study_uid,
        )

        # ── Step 3: Delegate to service ───────────────────────────────────────
        try:
            self._service.handle_message(request, raw)
            try:
                channel.basic_ack(delivery_tag=delivery_tag)
                logger.info("ACK sent for request_id=%s", request.request_id)
            except Exception as ack_exc:
                # Canal fechado porque a conexão TCP caiu junto com o publish.
                # RabbitMQ vai reenviar a mensagem ao reconectar — o INSERT
                # idempotente (WHERE NOT EXISTS) protege contra duplicata.
                logger.warning(
                    "ACK falhou para request_id=%s (canal fechado — conexão perdida): %s. "
                    "Mensagem será reenviada pelo broker; processamento já concluído no banco.",
                    request.request_id,
                    ack_exc,
                )
        except Exception as exc:
            logger.error(
                "Unhandled error processing request_id=%s — NACKing: %s",
                request.request_id,
                exc,
                exc_info=True,
            )
            # Requeue only if retry_count allows it (prevents infinite loops)
            requeue = request.retry_count < 3
            try:
                channel.basic_nack(delivery_tag=delivery_tag, requeue=requeue)
            except Exception as nack_exc:
                logger.warning(
                    "NACK também falhou para request_id=%s (canal fechado): %s",
                    request.request_id,
                    nack_exc,
                )

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start_consuming(self) -> None:
        """
        Blocking consume loop with automatic reconnection on failures.
        Runs until stop() is called or the process is terminated.
        """
        self._running = True
        retry_delay = self._settings.rabbitmq_retry_delay
        logger.info("Starting consumer on queue: %s", self._settings.rabbitmq_queue_input)

        while self._running:
            try:
                self._conn.ensure_connected()
                self._declare_queues()
                channel = self._conn.channel()
                channel.basic_consume(
                    queue=self._settings.rabbitmq_queue_input,
                    on_message_callback=self._on_message,
                    auto_ack=False,
                )
                logger.info("Waiting for messages …")
                channel.start_consuming()

            except pika.exceptions.ConnectionClosedByBroker as exc:
                if not self._running:
                    break
                logger.warning("Broker closed connection: %s — reconnecting …", exc)
                time.sleep(retry_delay)

            except pika.exceptions.AMQPChannelError as exc:
                if not self._running:
                    break
                logger.error("AMQP channel error: %s — reconnecting …", exc)
                time.sleep(retry_delay)

            except pika.exceptions.AMQPConnectionError as exc:
                if not self._running:
                    break
                logger.warning("AMQP connection error: %s — reconnecting in %.0fs …", exc, retry_delay)
                time.sleep(retry_delay)

            except KeyboardInterrupt:
                logger.info("Consumer interrupted by keyboard — stopping")
                self._running = False
                break

            except Exception as exc:
                if not self._running:
                    break
                logger.error("Unexpected consumer error: %s — retrying in %.0fs", exc, retry_delay, exc_info=True)
                time.sleep(retry_delay)

        logger.info("Consumer stopped")

    def stop(self) -> None:
        """Signal the consumer loop to stop gracefully."""
        self._running = False
        self._conn.request_stop_consuming()
