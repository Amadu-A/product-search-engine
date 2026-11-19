# app/application/report.py
"""Формирование проверяемого Markdown-отчёта по фактическим SQL-данным и аудиту."""

import json
from collections import Counter
from datetime import UTC, datetime

from app.application.ports import ReportRepository
from app.observability import timed


class BuildReport:
    """Показывает отдельно состояние БД, результаты импортов и непроверенные условия."""

    def __init__(self, repository: ReportRepository) -> None:
        """Получает источник SQL-фактов через application port."""
        self.repository = repository

    @timed("taxonomy_report")
    def execute(self, commit_sha: str, context: dict) -> str:
        """Возвращает самостоятельный отчёт; не объявляет импорт без успешного audit run."""
        data = self.repository.inspect()
        counts = data["counts"]
        history = data["history"]
        status = "IMPORTED" if history and counts["errors"] == 0 else "NOT_IMPORTED"
        if counts["errors"]:
            status = "ERROR"
        levels = Counter()
        for row in data["structure"]:
            levels[row["level"]] += row["count"]
        lines = [
            "<!-- .tmp/reports/petrovich_taxonomy_import.md -->",
            "# Отчёт импорта классификатора",
            "",
            f"Дата UTC: {datetime.now(UTC).isoformat()}",
            f"Commit: `{commit_sha}`",
            f"Статус: **{status}**",
            "",
            "Метрики ниже рассчитаны SQL в одной read-only транзакции. "
            "Неполнота и качество названий отражаются отдельно от структурных ошибок.",
            "",
            "| Показатель | Значение |",
            "| --- | ---: |",
        ]
        lines.extend(f"| {key} | {value} |" for key, value in counts.items())
        lines.extend(["", "## Уровни", "", "| Уровень (корень = 1) | Узлов |", "| --- | ---: |"])
        lines.extend(f"| {level} | {count} |" for level, count in sorted(levels.items()))
        lines.extend(
            [
                "",
                "## Структура по корневым ветвям",
                "",
                "```json",
                json.dumps(data["structure"], ensure_ascii=False, indent=2),
                "```",
            ]
        )
        lines.extend(
            [
                "",
                "## Источники и условия использования",
                "",
                "```json",
                json.dumps(data["sources"], ensure_ascii=False, indent=2),
                "```",
            ]
        )
        for saved in data["snapshots"]:
            payload = saved["payload"]
            lines.extend(
                [
                    "",
                    f"Снимок `{saved['sha256']}`",
                    "",
                    "```json",
                    json.dumps(
                        {
                            "source": payload["source"],
                            "method": payload["method"],
                            "extracted_at": payload["extracted_at"],
                            "raw_sha256": payload["raw_sha256"],
                            "files": payload["files"],
                            "complete": payload["complete"],
                            "diagnostics": payload.get("diagnostics"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "```",
                ]
            )
        lines.extend(["", "## Реальные операции apply (последние 20)", ""])
        if not history:
            lines.append(
                "Успешных импортов нет. Счётчики before/after и повторного apply отсутствуют."
            )
        for run in history:
            result = run["result"]
            previous = next(
                (
                    r
                    for r in history
                    if r["source"] == run["source"]
                    and (
                        r["snapshot_sha256"] == run["snapshot_sha256"]
                        or (
                            result.get("metrics", {}).get("input_sha256") is not None
                            and r["result"].get("metrics", {}).get("input_sha256")
                            == result["metrics"]["input_sha256"]
                        )
                    )
                    and r["created_at"] < run["created_at"]
                ),
                None,
            )
            repeated = previous is not None and result["added"] == result["updated"] == 0
            lines.extend(
                [
                    "```json",
                    json.dumps(
                        {
                            **run,
                            "repeated_unchanged": repeated,
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
                "## Подозрительные категории в PostgreSQL",
                "",
                "Цена или признак коллекции требует проверки. Пустые flags также не означают "
                "подтверждённую достоверность категории. Активность и названия не менялись.",
                "",
                "```json",
                json.dumps(
                    data.get("quality_nodes", []), ensure_ascii=False, indent=2, default=str
                ),
                "```",
                "",
            ]
        )
        lines.extend(
            [
                "## Проверки и ограничения",
                "",
                "```json",
                json.dumps(context, ensure_ascii=False, indent=2, default=str),
                "```",
                "",
                "complete сохраняется как во входном JSON; "
                "отчёт сборщика отражён в снимке и аудите. "
                "SQL проверяет фактическую связность БД, но не доказывает полноту чужого каталога. "
                "UNKNOWN/unverified и отсутствие уведомления информационны "
                "и не запрещают загрузку. "
                "Пропавшие категории сохраняются; custom не заменяются. "
                "Повторный apply здесь отражается только при наличии двух реальных audit runs.",
                "",
                "## Воспроизведение",
                "",
                "`bash scripts/test.sh` — изолированные тесты; "
                "`docker compose --env-file .env.example --env-file .env "
                "run --rm --no-deps app report` "
                "— повторить этот отчёт.",
                "",
            ]
        )
        return "\n".join(lines)
