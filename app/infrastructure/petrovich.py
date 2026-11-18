# app/infrastructure/petrovich.py
"""Подключаемый адаптер Петровича: только разрешённый root-запрос, без обхода блокировок."""

import hashlib
import ipaddress
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.domain.taxonomy import Permission, TaxonomyError
from app.settings import Settings


class PetrovichAdapter:
    """Ограниченный HTTPS-клиент одного host; массовое копирование не реализовано."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        """Принимает настройки и тестовый транспорт; production не наследует proxy/cookies."""
        self.settings = settings
        self.client = client
        self.last_request = 0.0

    def _check_host(self, url: str) -> None:
        """Исключает redirects, чужие hosts, порты и private/metadata IP."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "petrovich.ru" or parsed.query:
            raise TaxonomyError("Запрещённый URL источника")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise TaxonomyError("Источник разрешился в непубличный адрес")

    def _request(self, client: httpx.Client, path: str) -> bytes:
        """Ограничивает размер, время и retry; 403/429/redirect никогда не повторяются."""
        url = self.settings.source_base_url + path
        self._check_host(url)
        for attempt in range(self.settings.http_retries + 1):
            interval = self.settings.http_min_interval_seconds - (
                time.monotonic() - self.last_request
            )
            if interval > 0:
                time.sleep(interval)
            self.last_request = time.monotonic()
            try:
                with client.stream("GET", url) as response:
                    if response.status_code in {401, 403, 429} or response.is_redirect:
                        raise TaxonomyError(f"BLOCKED: источник вернул HTTP {response.status_code}")
                    if response.status_code >= 500 and attempt < self.settings.http_retries:
                        continue
                    if response.status_code != 200:
                        raise TaxonomyError(f"BLOCKED: источник вернул HTTP {response.status_code}")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.settings.http_max_response_bytes:
                            raise TaxonomyError("Превышен лимит ответа")
                        if (
                            time.monotonic() - self.last_request
                            > self.settings.http_timeout_seconds
                        ):
                            raise TaxonomyError("Превышено полное время ответа")
                    return bytes(raw)
            except httpx.TransportError:
                if attempt == self.settings.http_retries:
                    raise TaxonomyError("BLOCKED: исчерпаны HTTP timeout/retry") from None
        raise TaxonomyError("BLOCKED: ответ не получен")

    def fetch_catalog(self, permission: Permission, output_dir: Path) -> Path:
        """Проверяет документированное разрешение и robots, сохраняет только каталог.

        Полнота дерева не предполагается. Для нормализации требуется распознанный
        структурированный экспорт и отдельный review. Никаких карточек и изображений.
        """
        if permission.source != "petrovich":
            raise TaxonomyError("Разрешение относится к другому источнику")
        permission.require("fetch")
        owned_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.settings.http_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": self.settings.http_user_agent},
        )
        try:
            robots_raw = self._request(client, "/robots.txt")
            robots = RobotFileParser()
            robots.parse(robots_raw.decode("utf-8").splitlines())
            catalog = self.settings.source_base_url + self.settings.source_catalog_path
            if not robots.can_fetch(self.settings.http_user_agent, catalog):
                raise TaxonomyError("BLOCKED: robots.txt запрещает получение каталога")
            delay = robots.crawl_delay(self.settings.http_user_agent) or robots.crawl_delay("*")
            if delay:
                self.last_request = time.monotonic() + delay
            raw = self._request(client, self.settings.source_catalog_path)
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / (hashlib.sha256(raw).hexdigest() + ".html")
            path.write_bytes(raw)
            return path
        finally:
            if owned_client:
                client.close()
