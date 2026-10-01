"""
RabbitMQ publisher.  Publishes WorkerResponse envelopes to the output queue.
Stateless — receives a channel from RabbitMQConnection each call.
"""
from __future__ import annotations

import json
import logging

import pika

from ..config.settings import WorkerSettings
from ..models.contracts import WorkerResponse
from .connection import RabbitMQConnection

logger = logging.getLogger("worker.messaging.publisher")


class RabbitMQPublisher:
    """
    Publishes serialized WorkerResponse messages.

    Declares the output queue (idempotent) and publishes with
    persistent delivery mode so messages survive broker restarts.
    """

    def __init__(self, settings: WorkerSettings, conn: RabbitMQConnection) -> None:
        self._settings = settings
        self._conn = conn

    def _ensure_queue(self, queue_name: str) -> None:
        channel = self._conn.channel()
        channel.queue_declare(queue=queue_name, durable=True)

    def publish(
        self,
        response: WorkerResponse,
        *,
        queue: str | None = None,
    ) -> None:
        """
        Serialize and publish a WorkerResponse.

        Args:
            response: The response envelope to publish.
            queue:    Override destination queue (uses settings.rabbitmq_queue_output by default).

        Raises:
            RuntimeError: on publish failure after channel recovery attempt.
        """
        target_queue = queue or self._settings.rabbitmq_queue_output
        body = response.model_dump_json(indent=None).encode("utf-8")
        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=response.request_id,
            correlation_id=response.correlation_id,
        )

        try:
            self._conn.ensure_connected()
            channel = self._conn.channel()
            self._ensure_queue(target_queue)
            channel.basic_publish(
                exchange="",
                routing_key=target_queue,
                body=body,
                properties=props,
            )
            logger.info(
                "Published response for request_id=%s to queue=%s status=%s",
                response.request_id,
                target_queue,
                response.status,
            )
        except Exception as exc:
            logger.error(
                "Failed to publish response for request_id=%s: %s",
                response.request_id,
                exc,
            )
            raise RuntimeError(f"Publish failed: {exc}") from exc
