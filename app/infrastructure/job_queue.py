# app/infrastructure/job_queue.py
"""Shared RabbitMQ transport и явный PostgreSQL-only режим изолированных тестов."""

import json
from uuid import uuid4

import pika

from app.domain.catalog import DependencyUnavailable


class JobQueue:
    """Никогда не создаёт пользователей, vhost или чужие очереди."""

    def __init__(self, settings):
        """Проверяет project namespace и получает секрет только через Settings."""
        self.settings = settings
        if (
            settings.rabbitmq_user in {"shared_admin", "shared-admin", "guest", "administrator"}
            or settings.rabbitmq_vhost == "/"
        ):
            raise ValueError("Runtime RabbitMQ требует отдельные project user/vhost")

    def connect(self):
        """Открывает ограниченное соединение только под project user."""
        password = self.settings.rabbitmq_password.get_secret_value()
        if not password:
            raise DependencyUnavailable("RABBITMQ_PASSWORD проекта не задан")
        try:
            return pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=self.settings.rabbitmq_host,
                    port=self.settings.rabbitmq_port,
                    virtual_host=self.settings.rabbitmq_vhost,
                    credentials=pika.PlainCredentials(self.settings.rabbitmq_user, password),
                    connection_attempts=1,
                    socket_timeout=5,
                    stack_timeout=10,
                    blocked_connection_timeout=10,
                    heartbeat=60,
                )
            )
        except pika.exceptions.AMQPError as error:
            raise DependencyUnavailable("Project RabbitMQ недоступен") from error

    def publish(self, job_id: str) -> None:
        """Publisher confirm подтверждает доставку; PG job остаётся durable при сбое."""
        if self.settings.job_transport == "postgres":
            return
        connection = self.connect()
        try:
            channel = connection.channel()
            channel.queue_declare(queue=self.settings.rabbitmq_queue, durable=True)
            channel.confirm_delivery()
            channel.basic_publish(
                exchange="",
                routing_key=self.settings.rabbitmq_queue,
                body=json.dumps({"job_id": job_id}).encode(),
                mandatory=True,
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )
        except pika.exceptions.AMQPError as error:
            raise DependencyUnavailable("Не подтверждена публикация задания") from error
        finally:
            connection.close()

    def check(self) -> str:
        """Проверяет AMQP roundtrip только в своей временной очереди project user."""
        if self.settings.job_transport == "postgres":
            return "postgres_transport"
        connection = self.connect()
        try:
            channel = connection.channel()
            name = self.settings.rabbitmq_queue + ".probe." + uuid4().hex
            channel.queue_declare(queue=name, exclusive=True, auto_delete=True)
            channel.basic_publish(exchange="", routing_key=name, body=b"project-connection-check")
            method, _, body = channel.basic_get(name, auto_ack=True)
            if method is None or body != b"project-connection-check":
                raise DependencyUnavailable("Project RabbitMQ roundtrip не подтверждён")
            channel.queue_delete(name)
            return "project_roundtrip_ok"
        finally:
            connection.close()

    def receive(self) -> tuple[object, object, str] | None:
        """Берёт одно сообщение с manual ACK и prefetch=1; PG claim защищает от повторов."""
        if self.settings.job_transport == "postgres":
            return None
        connection = self.connect()
        channel = connection.channel()
        channel.queue_declare(queue=self.settings.rabbitmq_queue, durable=True)
        channel.basic_qos(prefetch_count=1)
        method, _, body = channel.basic_get(self.settings.rabbitmq_queue, auto_ack=False)
        if method is None:
            connection.close()
            return None
        try:
            job_id = json.loads(body)["job_id"]
            from uuid import UUID

            UUID(job_id)
        except (ValueError, KeyError, TypeError):
            channel.basic_reject(method.delivery_tag, requeue=False)
            connection.close()
            return None
        return connection, (channel, method.delivery_tag), job_id
