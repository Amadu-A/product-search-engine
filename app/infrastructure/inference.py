# app/infrastructure/inference.py
"""OpenAI-совместимые shared-клиенты и явно маркированный изолированный mock."""

import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator

from app.domain.catalog import DependencyUnavailable, InvalidModelOutput, fingerprint
from app.settings import Settings


class SharedInference:
    """Использует только логические shared-контракты; не запускает собственные модели."""

    def __init__(self, settings: Settings):
        """Загружает неизменённые prompt/schema и ограниченные HTTP timeouts."""
        self.settings = settings
        self.prompts, self.schemas = {}, {}
        for step, name in ((1, "product_understanding"), (2, "category_selection")):
            self.prompts[step] = Path(f"docs/prompts/answer_{step}_{name}.txt").read_text(
                encoding="utf-8"
            )
            self.schemas[step] = json.loads(
                Path(f"docs/contracts/answer_{step}.schema.json").read_text(encoding="utf-8")
            )
        self.client = httpx.Client(
            timeout=settings.inference_timeout_seconds, follow_redirects=False
        )
        self.mode = settings.inference_mode
        self.probe_times = {}
        self.semaphore = threading.BoundedSemaphore(settings.inference_concurrency)
        self.model_revision = (
            "mock-v1" if self.mode == "mock" else settings.shared_vlm_model_revision
        )
        self.embedding_revision = (
            "mock-hash-v1" if self.mode == "mock" else settings.shared_embedding_model_revision
        )
        self.dimensions = (
            settings.mock_embedding_dimensions
            if self.mode == "mock"
            else settings.embedding_dimensions
        )

    def check_availability(self) -> None:
        """Проверяет живые logical models с отдельным коротким readiness timeout."""
        if self.mode == "mock":
            return
        for base, logical in (
            (self.settings.shared_vlm_base_url, self.settings.shared_vlm_model),
            (self.settings.shared_embedding_base_url, self.settings.shared_embedding_model),
        ):
            try:
                response = self.client.get(
                    base.rstrip("/") + "/models", timeout=self.settings.readiness_timeout_seconds
                )
                response.raise_for_status()
                if not any(row["id"] == logical for row in response.json()["data"]):
                    raise ValueError("Logical model отсутствует")
            except (httpx.HTTPError, KeyError, ValueError) as error:
                raise DependencyUnavailable("Shared logical model не готов") from error

    def request(self, url: str, payload: dict) -> dict:
        """Ограничивает суммарное число одновременных shared inference запросов."""
        with self.semaphore:
            return self._request(url, payload)

    def _request(self, url: str, payload: dict) -> dict:
        """Повторяет только timeout/429/5xx, ограниченно и без логирования payload."""
        for attempt in range(self.settings.inference_retries + 1):
            try:
                response = self.client.post(url, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.TransportError("Временная недоступность inference")
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.TimeoutException) as error:
                if attempt == self.settings.inference_retries:
                    raise DependencyUnavailable("shared inference недоступен") from error
                time.sleep(min(2**attempt, 4))
            except (httpx.HTTPStatusError, ValueError) as error:
                raise DependencyUnavailable("shared inference отклонил контракт запроса") from error
        raise DependencyUnavailable("shared inference недоступен")

    def provenance(self, step: int, values: dict, resolve: bool = True) -> dict:
        """Вычисляет ключ cache по input/prompt/model без подмены неизвестной revision."""
        if resolve:
            self.ensure_revision()
        return {
            "input_sha256": fingerprint(values),
            "prompt_sha256": hashlib.sha256(self.prompts[step].encode()).hexdigest(),
            "model_logical_id": self.settings.shared_vlm_model,
            "model_revision": self.model_revision,
        }

    def ensure_revision(self, embedding: bool = False) -> None:
        """Фиксирует наблюдаемую API revision, когда оператор не указал точную weights revision."""
        attribute = "embedding_revision" if embedding else "model_revision"
        revision = getattr(self, attribute)
        refresh = (
            revision.startswith("observed-api-")
            and time.monotonic() - self.probe_times.get(attribute, 0)
            > self.settings.taxonomy_reconcile_interval_seconds
        )
        if self.mode == "mock" or (revision != "unverified" and not refresh):
            return
        base = (
            self.settings.shared_embedding_base_url
            if embedding
            else self.settings.shared_vlm_base_url
        )
        logical = (
            self.settings.shared_embedding_model if embedding else self.settings.shared_vlm_model
        )
        try:
            response = self.client.get(base.rstrip("/") + "/models")
            response.raise_for_status()
            model = next(row for row in response.json()["data"] if row["id"] == logical)
            revision = (
                "observed-api-"
                + fingerprint({k: model.get(k) for k in ("id", "root", "created")})[:24]
            )
            setattr(self, attribute, revision)
            self.probe_times[attribute] = time.monotonic()
        except (httpx.HTTPError, ValueError, KeyError, StopIteration) as error:
            raise DependencyUnavailable(
                "Не удалось проверить логический shared model /models"
            ) from error

    def answer(self, step: int, values: dict) -> dict:
        """Проверяет schema, evidence и принадлежность выбранного UUID candidate-set."""
        metadata = self.provenance(step, values)
        prompt = self.render_prompt(step, values)
        if self.mode == "mock":
            answer = self.mock_answer(step, values)
            raw = json.dumps(answer, ensure_ascii=False)
        else:
            if self.model_revision == "unverified":
                raise DependencyUnavailable("SHARED_VLM_MODEL_REVISION не проверена оператором")
            result = self.request(
                self.settings.shared_vlm_base_url.rstrip("/") + "/chat/completions",
                {
                    "model": self.settings.shared_vlm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": self.settings.inference_max_tokens,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": f"answer_{step}", "schema": self.schemas[step]},
                    },
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            raw = None
            try:
                raw = result["choices"][0]["message"]["content"]
                answer = json.loads(raw)
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise InvalidModelOutput("invalid_output: модель не вернула JSON", raw) from error
        errors = list(Draft202012Validator(self.schemas[step]).iter_errors(answer))
        if errors:
            raise InvalidModelOutput("invalid_output: ответ не соответствует schema", raw)
        if step == 1:
            allowed = {e["evidence_id"] for e in values["evidence_bundle_json"]["evidence"]}
            used = set(answer["evidence_ids"])
            for attribute in answer["attributes"]:
                used.update(attribute["evidence_ids"])
        else:
            allowed = set(values["answer_1_json"]["evidence_ids"])
            for attribute in values["answer_1_json"]["attributes"]:
                allowed.update(attribute["evidence_ids"])
            used = set(answer["supporting_evidence_ids"])
            candidates = {str(c["category_id"]) for c in values["category_candidates_json"]}
            if not set(answer["compared_candidate_ids"]) <= candidates or (
                answer["category_id"] and answer["category_id"] not in candidates
            ):
                raise InvalidModelOutput("invalid_output: категория вне candidate-set", raw)
            if answer["status"] == "selected" and (
                not answer["category_id"] or answer["selection_type"] == "none"
            ):
                raise InvalidModelOutput("invalid_output: неполное решение selected", raw)
            if answer["status"] != "selected" and (
                answer["category_id"] is not None or answer["selection_type"] != "none"
            ):
                raise InvalidModelOutput("invalid_output: противоречивое решение", raw)
        if not used <= allowed:
            raise InvalidModelOutput("invalid_output: вымышленные evidence IDs", raw)
        if step == 1 and answer["status"] == "recognized" and not used:
            raise InvalidModelOutput("invalid_output: recognized без evidence", raw)
        return {**metadata, "answer_json": answer, "raw_response_text": raw, "mode": self.mode}

    def render_prompt(self, step: int, values: dict) -> str:
        """Подставляет только placeholders исходного шаблона, не интерпретируя их внутри данных."""
        pattern = r"\{\{([a-zA-Z0-9_]+)\}\}"
        if set(re.findall(pattern, self.prompts[step])) != set(values):
            raise ValueError("Набор placeholders промпта не соответствует контракту")
        return re.sub(
            pattern,
            lambda match: json.dumps(values[match.group(1)], ensure_ascii=False),
            self.prompts[step],
        )

    def mock_answer(self, step: int, values: dict) -> dict:
        """Синтетическая детерминированная заглушка; результат всегда помечен MOCK."""
        if step == 2:
            candidates = values["category_candidates_json"]
            selected = candidates[0]["category_id"] if candidates else None
            return {
                "status": "selected" if selected else "no_suitable_category",
                "category_id": selected,
                "selection_type": "broader" if selected else "none",
                "reason": "MOCK: синтетический выбор; требуется ручная проверка",
                "supporting_evidence_ids": values["answer_1_json"]["evidence_ids"],
                "compared_candidate_ids": [c["category_id"] for c in candidates],
            }
        bundle = values["evidence_bundle_json"]
        fields = bundle["normalized_fields"]
        name = fields.get("name") or ""
        listing = bool(re.search(r"каталог|цены от|купить.*недорого|коллекция", name, re.I))
        recognized = len(name.split()) >= 2 and not re.fullmatch(r"[\w-]{1,20}", name)
        return {
            "status": "recognized" if recognized else "insufficient_evidence",
            "record_kind": "category_listing"
            if listing
            else "product_candidate"
            if recognized
            else "unknown",
            "product_type": name.split()[0] if recognized else None,
            "generic_type": None,
            "purpose": None,
            "applications": [],
            "brand_candidate": fields.get("brand"),
            "manufacturer_candidate": fields.get("manufacturer"),
            "mpn_candidate": fields.get("mpn"),
            "attributes": [],
            "evidence_ids": [e["evidence_id"] for e in bundle["evidence"]],
            "reason": "MOCK: только тестовый ответ, не проверка модели",
        }

    def embed(self, documents: list[str]) -> list[list[float]]:
        """Проверяет фактическую размерность и конечность всех координат."""
        self.ensure_revision(embedding=True)
        if self.mode == "mock":
            vectors = []
            for document in documents:
                vector = [0.0] * self.dimensions
                for token in re.findall(r"\w+", document.casefold()):
                    digest = hashlib.sha256(token.encode()).digest()
                    vector[int.from_bytes(digest[:4]) % self.dimensions] += (
                        1 if digest[4] % 2 else -1
                    )
                norm = math.sqrt(sum(v * v for v in vector)) or 1
                vectors.append([v / norm for v in vector])
        else:
            if self.embedding_revision == "unverified":
                raise DependencyUnavailable(
                    "SHARED_EMBEDDING_MODEL_REVISION не проверена оператором"
                )
            result = self.request(
                self.settings.shared_embedding_base_url.rstrip("/") + "/embeddings",
                {
                    "model": self.settings.shared_embedding_model,
                    "input": documents,
                },
            )
            try:
                vectors = [
                    row["embedding"] for row in sorted(result["data"], key=lambda row: row["index"])
                ]
            except (KeyError, TypeError) as error:
                raise DependencyUnavailable("Embedding вернул неверный контракт") from error
        if len(vectors) != len(documents) or any(
            len(v) != self.dimensions
            or not all(isinstance(n, (float, int)) and math.isfinite(n) for n in v)
            for v in vectors
        ):
            raise DependencyUnavailable(
                "Фактическая размерность/числа embedding не соответствуют конфигурации"
            )
        return vectors
