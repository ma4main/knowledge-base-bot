"""Учёт расхода по моделям — чтобы сравнение опиралось на цифры, а не на ощущения.

Пишем строку на каждый ответ в JSONL. Формат выбран ради переносимости:
файл читается глазами и любым инструментом, отдельная БД на этих объёмах не нужна.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class Transcript:
    """Вопросы и ответы — чтобы качество модели можно было оценить, а не угадать.

    Лежит отдельно от журнала расхода, чтобы /stats оставался быстрым.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        user_id: int,
        user_name: str,
        model: str,
        question: str,
        answer: str,
        seconds: float,
        kind: str = "",
        units: list[str] | None = None,
    ) -> None:
        """`kind` — тип запроса по классификатору, `units` — единицы, на которых стоит ответ."""
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user_id": user_id,
            "user_name": user_name,
            "model": model,
            "question": question,
            "answer": answer,
            "seconds": round(seconds, 1),
            "kind": kind,
            "units": units or [],
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            log.error("Не смог записать переписку", exc_info=True)


    def top_questions(self, days: int = 30, limit: int = 15) -> list[tuple[str, int, list[str]]]:
        """Самые частые вопросы за период: [(вопрос, сколько раз, типы)].

        Одинаковые по смыслу вопросы схлопываются сравнением из `dedupe`;
        показывается формулировка, которая встретилась первой.
        """
        import dedupe  # локально: usage.py грузится и там, где dedupe не нужен

        if not self.path.is_file():
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days)
        groups: dict[str, dict] = {}
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if datetime.fromisoformat(row["ts"]) < since:
                        continue
                except Exception:
                    continue
                question = (row.get("question") or "").strip()
                if not question or question.startswith("/"):
                    continue
                # Обращения без единиц (бот не ответил по базе) в «частые вопросы» не идут.
                if not row.get("units"):
                    continue
                hit = dedupe.find_duplicate(question, {k: v["text"] for k, v in groups.items()})
                key = hit[0] if hit else question
                group = groups.setdefault(key, {"text": question, "count": 0, "kinds": []})
                group["count"] += 1
                kind = row.get("kind") or ""
                if kind and kind not in group["kinds"]:
                    group["kinds"].append(kind)
        ranked = sorted(groups.values(), key=lambda g: -g["count"])
        return [(g["text"], g["count"], g["kinds"]) for g in ranked[:limit] if g["count"] > 1]


class UsageLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, user_id: int, model: str, usage, seconds: float) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user_id": user_id,
            "model": model,
            "prompt_tokens": usage.prompt_tokens,
            "cached_tokens": usage.cached_tokens,
            "completion_tokens": usage.completion_tokens,
            "cost": round(usage.cost, 6),
            "cache_discount": round(usage.cache_discount, 6),
            "seconds": round(seconds, 1),
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            log.error("Не смог записать расход", exc_info=True)

    def summary(self, days: int = 30) -> str:
        if not self.path.is_file():
            return "Пока ни одного вопроса — сравнивать нечего."

        since = datetime.now(timezone.utc) - timedelta(days=days)
        stats: dict[str, dict[str, float]] = defaultdict(
            lambda: {"вопросов": 0, "стоимость": 0.0, "секунды": 0.0, "вход": 0, "кэш": 0}
        )

        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if datetime.fromisoformat(row["ts"]) < since:
                        continue
                except Exception:
                    continue  # битую строку молча пропускаем, статистика не критична
                entry = stats[row.get("model", "?")]
                entry["вопросов"] += 1
                entry["стоимость"] += row.get("cost", 0)
                entry["секунды"] += row.get("seconds", 0)
                entry["вход"] += row.get("prompt_tokens", 0)
                entry["кэш"] += row.get("cached_tokens", 0)

        if not stats:
            return f"За последние {days} дн. вопросов не было."

        lines = [f"<b>Расход за {days} дн.</b>", ""]
        total = 0.0
        for model, entry in sorted(stats.items(), key=lambda kv: -kv[1]["стоимость"]):
            count = int(entry["вопросов"])
            avg = entry["стоимость"] / count if count else 0
            speed = entry["секунды"] / count if count else 0
            share = 100 * entry["кэш"] / entry["вход"] if entry["вход"] else 0
            total += entry["стоимость"]
            lines.append(
                f"<b>{model}</b>\n"
                f"    вопросов: {count}, всего ${entry['стоимость']:.3f}\n"
                f"    в среднем ${avg:.4f} за вопрос, {speed:.0f} сек\n"
                f"    из кэша: {share:.0f}% входных токенов"
            )
        lines.append("")
        lines.append(f"<b>Итого: ${total:.2f}</b>")
        return "\n".join(lines)
