"""Обратная связь менеджеров: оценки ответов и предложения/идеи.

Пишем в JSONL (переносимо, читается глазами). Оценки — сигнал качества бота
на раннем этапе; предложения — чтобы идея не потерялась и её можно было обсудить.
"""

from __future__ import annotations

import html
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _esc(text: str) -> str:
    """Экранирование перед вставкой в HTML-сводку: иначе один символ < в записи ломает всю команду."""
    return html.escape(str(text or ""))


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, row: dict) -> None:
        row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            log.error("Не смог записать в %s", self.path, exc_info=True)

    def _rows(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out


class FeedbackLog(JsonlLog):
    def record(self, user_id: int, name: str, rating: str, question: str) -> None:
        self._write({"user_id": user_id, "name": name, "rating": rating, "question": question})
        log.info("Оценка от %s: %s (%s)", user_id, rating, question[:80])

    def summary(self) -> str:
        rows = self._rows()
        if not rows:
            return "Оценок пока нет."
        real, testing = _split_testing(rows)
        counts = Counter(r.get("rating", "?") for r in real)
        lines = [f"<b>Оценки ответов</b> (всего {len(real)})", ""]
        for rating, label in [("ok", "✅ Верно"), ("maybe", "🤔 Спорно"), ("no", "❌ Неверно")]:
            lines.append(f"{label}: {counts.get(rating, 0)}")
        wrong = [r for r in real if r.get("rating") in {"no", "maybe"}]
        if wrong:
            lines.append("\n<b>Спорные/неверные — на разбор:</b>")
            for r in wrong[-15:]:
                mark = "❌" if r["rating"] == "no" else "🤔"
                lines.append(f"{mark} {_esc(r.get('question', '')[:90])}")
        if testing:
            lines.append(
                f"\n<i>Отложено как проверка бота на прочность: {len(testing)} "
                f"оценок подряд от одного человека.</i>"
            )
        return "\n".join(lines)


# Сколько оценок подряд от одного человека считаем не обратной связью, а тестом.
_TESTING_STREAK = 4


def _split_testing(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Делит оценки на настоящие и похожие на прогон «а что он ответит» (серия от одного человека в один день)."""
    real: list[dict] = []
    testing: list[dict] = []
    streak: list[dict] = []

    def flush() -> None:
        (testing if len(streak) >= _TESTING_STREAK else real).extend(streak)
        streak.clear()

    for row in rows:
        same = (
            streak
            and row.get("user_id") == streak[-1].get("user_id")
            and (row.get("ts") or "")[:10] == (streak[-1].get("ts") or "")[:10]
        )
        if not same:
            flush()
        streak.append(row)
    flush()
    return real, testing


class SuggestionLog(JsonlLog):
    def record(self, user_id: int, name: str, text: str) -> None:
        self._write({"user_id": user_id, "name": name, "text": text})
        log.info("Предложение от %s (%s): %s", user_id, name, text[:100])

    def summary(self) -> str:
        rows = self._rows()
        if not rows:
            return "Предложений пока нет."
        lines = [f"<b>Предложения менеджеров</b> ({len(rows)})", ""]
        for r in rows[-20:]:
            when = r.get("ts", "")[:10]
            lines.append(f"• <i>{_esc(r.get('name', '?'))}, {when}:</i> {_esc(r.get('text', ''))}")
        return "\n".join(lines)
