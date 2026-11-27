# app/infrastructure/inference.py
"""OpenAI-совместимые shared-клиенты и явно маркированный изолированный mock."""

import hashlib
import json
import math
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator

from app.domain.catalog import DependencyUnavailable, InvalidModelOutput, fingerprint
from app.domain.product_identity import IDENTIFIERS, literal
from app.settings import Settings


class SharedInference:
    """Использует только логические shared-контракты; не запускает собственные модели."""

    def __init__(self, settings: Settings):
        """Загружает неизменённые prompt/schema и ограниченные HTTP timeouts."""
        self.settings = settings
        self.prompts, self.schemas = {}, {}
        for step, name in (
            (1, "product_understanding"),
            (2, "category_selection"),
            (3, "hierarchical_selection"),
        ):
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
        self.usage = ContextVar("classification_vlm_usage", default=None)
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

    @contextmanager
    def budget(self):
        """Ограничивает все запросы завершения, включая повтор HTTP и исправление JSON."""
        usage = {"vlm_requests": 0}
        token = self.usage.set(usage)
        try:
            yield usage
        finally:
            self.usage.reset(token)

    def consume_request(self) -> None:
        """Считает только запросы VLM текущей позиции, отдельно от embedding."""
        usage = self.usage.get()
        if usage is not None:
            if usage["vlm_requests"] >= self.settings.classification_vlm_budget:
                raise DependencyUnavailable("Исчерпан бюджет VLM на позицию")
            usage["vlm_requests"] += 1

    def _request(self, url: str, payload: dict) -> dict:
        """Повторяет только timeout/429/5xx, ограниченно и без логирования payload."""
        for attempt in range(self.settings.inference_retries + 1):
            if url.endswith("/chat/completions"):
                self.consume_request()
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
        """Валидирует ответ, при технической ошибке запрашивает ровно одно исправление."""
        metadata = self.provenance(step, values)
        prompt = self.render_prompt(step, values)
        attempts = []
        for _ in range(2):
            try:
                raw, finish = self.completion(step, values, prompt)
                answer = self.validate_answer(step, values, raw, finish)
            except InvalidModelOutput as error:
                attempts.append(error.diagnostic())
                if len(attempts) == 2 or self.mode == "mock":
                    error.attempts = attempts
                    raise
                prompt += (
                    "\n\nПредыдущий ответ отклонён. Исправь следующую ошибку: "
                    + str(error)
                    + ". Верни только полный JSON согласно схеме и исходным ограничениям."
                )
            except DependencyUnavailable as error:
                if attempts:
                    invalid = InvalidModelOutput(str(error), None, code="technical", step=step)
                    invalid.attempts = [*attempts, invalid.diagnostic()]
                    raise invalid from error
                raise
            else:
                return {
                    **metadata,
                    "answer_json": answer,
                    "raw_response_text": raw,
                    "finish_reason": finish,
                    "attempts": attempts,
                    "mode": self.mode,
                }
        raise AssertionError("Не достигнут результат валидации")

    def completion(self, step: int, values: dict, prompt: str) -> tuple[object, str | None]:
        """Читает финальный content и finish_reason одного завершения."""
        if self.mode == "mock":
            self.consume_request()
            return json.dumps(self.mock_answer(step, values), ensure_ascii=False), "stop"
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
        try:
            choice = result["choices"][0]
            return choice["message"]["content"], choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as error:
            raise InvalidModelOutput(
                "Неверная структура ответа API", result, code="technical", step=step
            ) from error

    def validate_answer(self, step: int, values: dict, raw: object, finish: str | None) -> dict:
        """Проверяет JSON, схему, UUID, evidence и согласованность полей."""

        def invalid(code: str, message: str) -> InvalidModelOutput:
            """Сохраняет исходный ответ, причину и этап."""
            return InvalidModelOutput(message, raw, code=code, step=step, finish_reason=finish)

        if finish == "length":
            raise invalid("truncated", "Генерация оборвалась по лимиту")
        if finish not in {None, "stop"}:
            raise invalid("technical", "API завершил генерацию без штатного ответа")
        try:
            answer = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise invalid("json", "Модель не вернула корректный JSON") from error
        error = next(Draft202012Validator(self.schemas[step]).iter_errors(answer), None)
        if error:
            field = ".".join(map(str, error.absolute_path)) or "корень"
            suffix = (
                f" ({', '.join(sorted(set(error.validator_value) - answer.keys()))})"
                if (error.validator == "required" and isinstance(answer, dict))
                else ""
            )
            raise invalid("schema", f"Нарушена JSON Schema: {field}, {error.validator}{suffix}")
        if step in {2, 3}:
            identifiers = [answer["category_id"]] if answer["category_id"] is not None else []
            identifiers += answer.get("compared_candidate_ids", [])
            try:
                for identifier in identifiers:
                    UUID(identifier)
            except ValueError as error:
                raise invalid("uuid", "Неверный формат UUID категории") from error
        if step == 3:
            current = values["current_category_json"]
            actions = {
                "select_child": {c["category_id"] for c in values["child_categories_json"]},
                "select_current": {current["category_id"]}
                if current and current["is_assignable"]
                else set(),
                "no_match": {None},
            }
            if answer["category_id"] not in actions[answer["action"]]:
                raise invalid("uuid", "UUID не разрешён для действия на этом уровне")
            return answer
        if step == 1:
            sources = {
                e["evidence_id"]: e["text"] for e in values["evidence_bundle_json"]["evidence"]
            }
            allowed = set(sources)
            used = set(answer["evidence_ids"])
            for kind, claim in answer["identifiers"].items():
                if claim is None:
                    continue
                refs = set(claim["evidence_ids"])
                used.update(refs)
                if claim["origin"] == "inferred" and refs:
                    raise invalid("evidence", "Предположение не должно ссылаться на исходный факт")
                if claim["origin"] == "source" and not any(
                    ref in {"raw_name", f"raw_{kind}"}
                    and literal(claim["value"], sources.get(ref, ""))
                    for ref in refs
                ):
                    raise invalid("evidence", "Идентификатор отсутствует в указанном исходном поле")
            for attribute in answer["attributes"]:
                used.update(attribute["evidence_ids"])
            if answer["status"] == "recognized" and not used:
                raise invalid("evidence", "recognized без evidence")
            answer["attributes"] = [
                entry
                for entry in answer["attributes"]
                if "raw_name" in entry["evidence_ids"]
                and any(
                    literal(value, sources.get("raw_name", ""))
                    for value in (
                        entry["value"],
                        entry["value"] + (entry["unit"] or ""),
                        entry["value"] + " " + (entry["unit"] or ""),
                    )
                )
            ]
            for kind in ("brand", "manufacturer", "mpn"):
                identity = answer["identifiers"][kind]
                answer[f"{kind}_candidate"] = (
                    identity["value"] if identity and identity["origin"] == "source" else None
                )
        else:
            allowed = set(values["answer_1_json"]["evidence_ids"])
            for attribute in values["answer_1_json"]["attributes"]:
                allowed.update(attribute["evidence_ids"])
            used = set(answer["supporting_evidence_ids"])
            candidates = {str(c["category_id"]) for c in values["category_candidates_json"]}
            if not set(identifiers) <= candidates:
                raise invalid("uuid", "Категория вне candidate-set")
            if (answer["status"] == "selected") != (
                answer["category_id"] is not None and answer["selection_type"] != "none"
            ) or (
                answer["status"] != "selected"
                and (answer["category_id"] is not None or answer["selection_type"] != "none")
            ):
                raise invalid("status", "Противоречивые status, category_id и selection_type")
        if not used <= allowed:
            raise invalid("evidence", "Недопустимые evidence IDs")
        return answer

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
        if step == 3:
            return {"action": "no_match", "category_id": None, "reason": "MOCK: нет совпадения"}
        if step == 2:
            candidates = values["category_candidates_json"]
            selected = candidates[0]["category_id"] if candidates else None
            return {
                "status": "selected" if selected else "no_suitable_category",
                "category_id": selected,
                "selection_type": "broader" if selected else "none",
                "reason": "MOCK: синтетический выбор",
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
            "identifiers": {
                key: {"value": fields[key], "origin": "source", "evidence_ids": [f"raw_{key}"]}
                if fields.get(key)
                else None
                for key in (*IDENTIFIERS, "seller_code")
            },
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
