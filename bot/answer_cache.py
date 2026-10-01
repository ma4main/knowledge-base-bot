"""Кэш ответов на повторяющиеся вопросы: второй менеджер получает готовый ответ.

Совпадение только жёсткое (нормализованный вопрос целиком); сброс адресный — по единицам,
на которых построен ответ; кэшируется только первый вопрос разговора; у ответа есть срок.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from kb import KB_ID_RE

log = logging.getLogger(__name__)


def _changed_units(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Какие единицы изменились, появились или исчезли между двумя снимками базы."""
    changed = {unit_id for unit_id, digest in after.items() if before.get(unit_id) != digest}
    changed |= {unit_id for unit_id in before if unit_id not in after}
    return changed

# Сообщения о сбоях самого бота — их кэшировать нельзя, это не ответы.
_FAILURES = ("Не смог сформулировать ответ", "Запутался в поиске по базе")

# Слова, которые не меняют смысл вопроса: «а какой тариф на гео?» и «какой тариф гео»
# должны попадать в один ключ. Список намеренно короткий — только служебные слова.
_STOP_WORDS = {
    "а", "и", "но", "же", "ли", "бы", "вот", "это", "этот", "эта", "то",
    "у", "в", "во", "на", "с", "со", "по", "из", "за", "от", "до", "для", "о", "об",
    "мне", "нам", "меня", "нас", "скажи", "подскажи", "пожалуйста", "плиз",
}

# Окончания, которые отрезаем у длинных слов: «тарифы»/«тарифа»/«тарифу» → «тариф».
# Полноценной морфологии тут не нужно — нужен устойчивый ключ.
_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ах", "ях", "ов", "ев",
    "ам", "ям", "ой", "ей", "ый", "ий", "ая", "яя", "ые", "ие", "ую", "юю",
    "а", "я", "ы", "и", "у", "ю", "е", "о",
)

MAX_ENTRIES = 500

# Срок жизни ответа: часть базы меняется вне бота.
MAX_AGE_DAYS = 30


def normalize(question: str) -> str:
    """Приводит вопрос к устойчивому ключу; порядок слов сохраняется."""
    text = question.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    words = []
    for word in text.split():
        if word in _STOP_WORDS:
            continue
        words.append(_stem(word))
    return " ".join(words)


def _stem(word: str) -> str:
    """Грубое отсечение окончания; короткие слова не трогаем."""
    if len(word) <= 4 or word.isdigit():
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[: -len(ending)]
    return word


@dataclass
class CachedAnswer:
    question: str  # исходная формулировка — для отладки и /stats
    answer: str
    model: str
    created_at: str
    hits: int = 0
    # На каких единицах базы построен ответ — по ним и выбрасываем адресно.
    # Пустой список = зависимость неизвестна, такой ответ выбрасываем при любой правке.
    units: list[str] = field(default_factory=list)
    # Тип запроса (qtype) — для кнопки «Подробнее» под ответом из кэша; пустая строка — старый формат записи.
    kind: str = ""

    def is_stale(self, max_age_days: int) -> bool:
        try:
            age = date.today() - date.fromisoformat(self.created_at)
        except ValueError:
            return True  # непонятная дата — считаем протухшим
        return age.days > max_age_days


@dataclass
class Stats:
    hits: int = 0
    misses: int = 0
    saved: int = 0  # сколько ответов сохранено за жизнь процесса
    dropped: int = 0  # выброшено по оценке «неверно»
    invalidated: int = 0  # выброшено правками базы (адресно)
    expired: int = 0  # выброшено по сроку


class AnswerCache:
    def __init__(
        self,
        path: Path,
        unit_hashes: dict[str, str],
        limit: int = MAX_ENTRIES,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> None:
        self.path = path
        self.limit = limit
        self.max_age_days = max_age_days
        self.unit_hashes = dict(unit_hashes)
        self.entries: dict[str, CachedAnswer] = {}
        self.stats = Stats()
        self._load()

    # --- Хранение ---

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Файл кэша ответов повреждён — начинаю с пустого", exc_info=True)
            return
        for key, value in (raw.get("entries") or {}).items():
            try:
                self.entries[key] = CachedAnswer(**value)
            except TypeError:
                continue  # запись из старой версии формата — пропускаем
        # База могла измениться, пока бот не работал: выбрасываем только затронутые ответы.
        stored = raw.get("unit_hashes") or {}
        changed = _changed_units(stored, self.unit_hashes)
        if changed:
            dropped = self._drop_dependent(changed)
            log.info(
                "База менялась вне бота: изменено единиц %d → выброшено ответов %d, осталось %d",
                len(changed), dropped, len(self.entries),
            )
        log.info("Кэш ответов загружен: %d записей", len(self.entries))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "unit_hashes": self.unit_hashes,
                        "entries": {k: asdict(v) for k, v in self.entries.items()},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            log.error("Не смог сохранить кэш ответов в %s", self.path, exc_info=True)

    # --- Работа ---

    def sync(self, unit_hashes: dict[str, str]) -> int:
        """Сверяет базу по единицам и выбрасывает только зависимые ответы. Возвращает, сколько выброшено."""
        changed = _changed_units(self.unit_hashes, unit_hashes)
        self.unit_hashes = dict(unit_hashes)
        if not changed:
            self._save()
            return 0
        dropped = self._drop_dependent(changed)
        self._save()
        log.info(
            "Правка базы (%s) → выброшено ответов: %d, осталось %d",
            ", ".join(sorted(changed)[:5]), dropped, len(self.entries),
        )
        return dropped

    def clear(self, unit_hashes: dict[str, str], reason: str) -> int:
        """Сбрасывает кэш целиком: при появлении новой единицы старый ответ «в базе этого нет» ни на что не ссылается."""
        had = len(self.entries)
        self.entries.clear()
        self.unit_hashes = dict(unit_hashes)
        self.stats.invalidated += had
        self._save()
        log.info("Кэш ответов сброшен целиком (%s): было %d записей", reason, had)
        return had

    def _drop_dependent(self, changed: set[str]) -> int:
        """Выбрасывает ответы, опирающиеся на изменённые единицы, и ответы без записанных зависимостей."""
        doomed = [
            key
            for key, entry in self.entries.items()
            if not entry.units or (set(entry.units) & changed)
        ]
        for key in doomed:
            del self.entries[key]
        self.stats.invalidated += len(doomed)
        return len(doomed)

    def _key(self, question: str, model: str, scope: str = "") -> str:
        # Модель и место (scope) — часть ключа: ответы отличаются формулировкой и длиной.
        return f"{model}|{scope}|{normalize(question)}"

    def get(self, question: str, model: str, scope: str = "") -> CachedAnswer | None:
        """Запись целиком: вызывающему нужны и единицы, и тип запроса."""
        key = self._key(question, model, scope)
        entry = self.entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.is_stale(self.max_age_days):
            del self.entries[key]
            self.stats.expired += 1
            self.stats.misses += 1
            self._save()
            log.info("Ответ из кэша протух (%s), пересчитаю: %s", entry.created_at, entry.question[:60])
            return None
        entry.hits += 1
        self.stats.hits += 1
        log.info(
            "Ответ из кэша (спрашивали %d раз, единицы %s): %s",
            entry.hits, ", ".join(entry.units) or "неизвестны", entry.question[:80],
        )
        self._save()
        return entry

    def put(
        self,
        question: str,
        answer: str,
        model: str,
        units: list[str] | None = None,
        kind: str = "",
        scope: str = "",
    ) -> bool:
        """Сохраняет ответ. False — если сохранять не стали. `units` — единицы, реально
        загруженные моделью; без них kb-id ищутся в тексте ответа (запасной путь)."""
        if not answer.strip() or any(f in answer for f in _FAILURES):
            return False
        key = self._key(question, model, scope)
        if not key.rsplit("|", 1)[1]:
            return False  # вопрос из одних служебных слов — ключа нет
        self.entries[key] = CachedAnswer(
            question=question.strip()[:300],
            answer=answer,
            model=model,
            created_at=date.today().isoformat(),
            units=sorted(set(units if units is not None else KB_ID_RE.findall(answer))),
            kind=kind,
        )
        self.stats.saved += 1
        # Кэш не растим бесконечно: выкидываем самые старые записи.
        while len(self.entries) > self.limit:
            self.entries.pop(next(iter(self.entries)))
        self._save()
        return True

    def drop(self, question: str, model: str, scope: str = "") -> bool:
        """Убирает ответ из кэша — например, когда его оценили как неверный."""
        if self.entries.pop(self._key(question, model, scope), None) is None:
            return False
        self.stats.dropped += 1
        self._save()
        log.info("Ответ убран из кэша по оценке «неверно»: %s", question[:80])
        return True

    def summary(self) -> str:
        total = self.stats.hits + self.stats.misses
        share = f"{100 * self.stats.hits / total:.0f}%" if total else "—"
        lines = [
            "<b>Кэш ответов</b>",
            f"В кэше: {len(self.entries)} вопросов",
            f"Отдано из кэша: {self.stats.hits} из {total} ({share})",
            f"Выброшено правками базы (адресно): {self.stats.invalidated}",
            f"Выброшено по сроку ({self.max_age_days} дн.): {self.stats.expired}",
            f"Убрано по оценке «неверно»: {self.stats.dropped}",
        ]
        top = sorted(self.entries.values(), key=lambda e: -e.hits)[:5]
        popular = [e for e in top if e.hits]
        if popular:
            lines.append("\n<b>Чаще всего спрашивают:</b>")
            lines.extend(f"• {e.question[:70]} — {e.hits} раз" for e in popular)
        return "\n".join(lines)
