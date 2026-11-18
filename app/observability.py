# app/observability.py
"""Единое измерение sync-операций CLI без повторного traceback и записи секретов."""

import logging
from collections.abc import Callable
from functools import wraps
from time import perf_counter
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def timed(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Создаёт timing-декоратор для синхронных публичных use-case этого этапа."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        """Сохраняет метаданные и сигнатуру измеряемой операции."""

        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            """Пишет один итоговый timing и сохраняет исходное исключение."""
            start = perf_counter()
            status = "error"
            try:
                result = function(*args, **kwargs)
                status = "success"
                return result
            finally:
                logging.getLogger(function.__module__).info(
                    "event=operation_timing operation=%s duration_ms=%.2f status=%s",
                    operation,
                    (perf_counter() - start) * 1000,
                    status,
                )

        return wrapped

    return decorate
