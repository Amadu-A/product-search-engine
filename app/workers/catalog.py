# app/workers/catalog.py
"""Checkpoint worker; закрытие браузера не останавливает обработку."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.catalog_bootstrap import build_catalog
from app.domain.catalog import DependencyUnavailable
from app.settings import Settings


def run(settings: Settings | None = None, once=False) -> dict:
    """Ограничивает concurrency, восстанавливает lease и выполняет startup reconciliation."""
    settings = settings or Settings()
    service = build_catalog(settings)
    last_sync = 0.0
    last_delivery = 0.0
    last_cleanup = 0.0
    futures = set()
    with ThreadPoolExecutor(max_workers=settings.worker_concurrency) as pool:
        while True:
            Path(".tmp/reports/worker-heartbeat").touch()
            service.store.recover(settings.worker_lease_seconds)
            if time.monotonic() - last_cleanup >= 300:
                service.cleanup_uploads()
                last_cleanup = time.monotonic()
            pending = service.index.status()["retryable_events"]
            interval = 5 if pending else settings.taxonomy_reconcile_interval_seconds
            if settings.taxonomy_index_worker_enabled and time.monotonic() - last_sync >= interval:
                try:
                    service.index.sync()
                except DependencyUnavailable:
                    logging.warning("Индекс недоступен; SQL и raw сохранены")
                last_sync = time.monotonic()
            done = {future for future in futures if future.done()}
            for future in done:
                future.result()
            futures -= done
            if len(futures) < settings.worker_concurrency:
                if settings.job_transport == "rabbitmq":
                    try:
                        if time.monotonic() - last_delivery > 15:
                            with service.store.transaction() as unit:
                                queued = unit.all("ingest_jobs", status="queued")
                            for job in queued[:10]:
                                service.queue.publish(str(job["id"]))
                            last_delivery = time.monotonic()
                        message = service.queue.receive()
                        if message:
                            connection, (channel, tag), identifier = message
                            claimed = service.store.claim_job(identifier)
                            if claimed:
                                futures.add(pool.submit(service.process, claimed))
                            channel.basic_ack(tag)
                            connection.close()
                    except DependencyUnavailable:
                        logging.warning("Project RabbitMQ недоступен; queued jobs сохранены")
                else:
                    claimed = service.store.claim_job()
                    if claimed:
                        futures.add(pool.submit(service.process, claimed))
            if once:
                for future in futures:
                    future.result()
                return {"status": "worker_once_completed", "mode": settings.inference_mode}
            time.sleep(settings.worker_poll_seconds)
