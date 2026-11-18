# tests/test_http.py
"""Offline HTTP-contract: соблюдение robots, отказ от 403/redirect и ограничение ответа."""

from datetime import UTC, datetime

import httpx
import pytest

from app.domain.taxonomy import Permission, TaxonomyError
from app.infrastructure.petrovich import PetrovichAdapter
from app.settings import Settings


@pytest.mark.parametrize("status", [403, 401, 429, 302])
def test_blocked_response_never_bypassed(monkeypatch, tmp_path, status):
    """Запрет на robots останавливает всё; повторов и запроса каталога нет."""
    visited = []

    def handler(request):
        """Фиксирует точные URL, возвращая заданную блокировку."""
        visited.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://example.org"})

    permission = Permission(
        source="petrovich",
        approved_by="Тестовый оператор",
        approved_at=datetime.now(UTC),
        reference="Синтетическое разрешение только для MockTransport",
        allow_fetch=True,
        allow_reuse=False,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = PetrovichAdapter(Settings(_env_file=None), client)
        monkeypatch.setattr(adapter, "_check_host", lambda url: None)
        with pytest.raises(TaxonomyError, match="BLOCKED"):
            adapter.fetch_catalog(permission, tmp_path)
    assert visited == ["https://petrovich.ru/robots.txt"]
    assert not list(tmp_path.iterdir())


def test_robots_disallow_blocks_catalog(monkeypatch, tmp_path):
    """Даже положительный документ не отменяет запрет robots.txt."""
    visited = []

    def handler(request):
        """Возвращает запрет автоматического получения для всех клиентов."""
        visited.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /catalog/\n")

    permission = Permission(
        source="petrovich",
        approved_by="Тестовый оператор",
        approved_at=datetime.now(UTC),
        reference="Синтетический документ только для unit-test",
        allow_fetch=True,
        allow_reuse=False,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = PetrovichAdapter(Settings(_env_file=None), client)
        monkeypatch.setattr(adapter, "_check_host", lambda url: None)
        with pytest.raises(TaxonomyError, match="robots"):
            adapter.fetch_catalog(permission, tmp_path)
    assert visited == ["/robots.txt"]


def test_host_allowlist_rejects_private_and_foreign():
    """Произвольный URL, metadata IP и HTTP не проходят до сетевого запроса."""
    adapter = PetrovichAdapter(Settings(_env_file=None))
    for url in ("http://petrovich.ru/catalog/", "https://169.254.169.254/", "https://example.org/"):
        with pytest.raises(TaxonomyError):
            adapter._check_host(url)


@pytest.mark.parametrize("scenario", ["success", "oversize", "timeout", "server_error"])
def test_bounded_http_contract(monkeypatch, tmp_path, scenario):
    """Разрешённый root сохраняется; размер и недоступность ограничивают число запросов."""
    paths = []

    def handler(request):
        """Моделирует ответы разрешённого источника без живых сетевых запросов."""
        paths.append(request.url.path)
        if scenario == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if scenario == "server_error":
            return httpx.Response(503)
        if scenario == "oversize":
            return httpx.Response(200, content=b"x" * 2048)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=b"<main>synthetic</main>")

    permission = Permission(
        source="petrovich",
        approved_by="Тестовый оператор",
        approved_at=datetime.now(UTC),
        reference="Синтетический документ исключительно для MockTransport",
        allow_fetch=True,
        allow_reuse=False,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = PetrovichAdapter(Settings(_env_file=None, http_max_response_bytes=1024), client)
        monkeypatch.setattr(adapter, "_check_host", lambda url: None)
        monkeypatch.setattr("app.infrastructure.petrovich.time.sleep", lambda seconds: None)
        if scenario == "success":
            path = adapter.fetch_catalog(permission, tmp_path)
            assert path.read_bytes() == b"<main>synthetic</main>"
            assert paths == ["/robots.txt", "/catalog/"]
        else:
            with pytest.raises(TaxonomyError):
                adapter.fetch_catalog(permission, tmp_path)
            assert len(paths) == (1 if scenario == "oversize" else 3)


def test_dns_private_address_is_blocked(monkeypatch):
    """Даже разрешённый hostname не может направить запрос на internal/metadata IP."""
    monkeypatch.setattr(
        "app.infrastructure.petrovich.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(TaxonomyError, match="непубличный"):
        PetrovichAdapter(Settings(_env_file=None))._check_host("https://petrovich.ru/catalog/")
