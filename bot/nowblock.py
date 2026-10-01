"""Блок «Сейчас»: единица хранит состояние, а не ленту событий.

Новый факт заменяет строку с тем же ключом (`- **Ключ:** значение — с ДД.ММ.ГГГГ`),
а старое значение уезжает в «Было раньше» с датой, по которую действовало.
Чистый разбор и сборка текста: ни модели, ни сети.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

HEADING_NOW = "## Сейчас"
HEADING_WAS = "## Было раньше"

# `- **Ключ:** значение` — двоеточие внутри жирного, чтобы ключ читался и глазами.
_LINE = re.compile(r"^-\s+\*\*(?P<key>[^*\n]+?):\*\*\s*(?P<value>.*?)\s*$")
# Хвост «— с ДД.ММ.ГГГГ» у действующей строки и «— по ДД.ММ.ГГГГ» у исторической.
_SINCE = re.compile(r"\s*—\s*с\s+(\d{2}\.\d{2}\.\d{4})\s*$")
_UNTIL = re.compile(r"\s*—\s*(?:действовало\s+)?по\s+(\d{2}\.\d{2}\.\d{4})\s*$")


@dataclass(frozen=True)
class Entry:
    """Строка блока «Сейчас»: сущность, её значение и с какого числа действует."""
    key: str
    value: str  # без хвоста с датой
    since: str  # ДД.ММ.ГГГГ или "" — если дату не проставили

    @property
    def norm_key(self) -> str:
        return normalize_key(self.key)


def normalize_key(key: str) -> str:
    """Ключи сравниваем мягко: без учёта регистра, «ё» и лишних пробелов."""
    return " ".join((key or "").lower().replace("ё", "е").split())


def today_ru() -> str:
    return date.today().strftime("%d.%m.%Y")


def has_now(text: str) -> bool:
    return _section_bounds(text, HEADING_NOW) is not None


def entries(text: str) -> list[Entry]:
    """Строки блока «Сейчас». Пустой список — если блока нет или он пуст."""
    bounds = _section_bounds(text, HEADING_NOW)
    if bounds is None:
        return []
    start, end = bounds
    found: list[Entry] = []
    for line in text[start:end].splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        value = match.group("value")
        since = ""
        tail = _SINCE.search(value)
        if tail:
            since = tail.group(1)
            value = value[: tail.start()].rstrip()
        found.append(Entry(match.group("key").strip(), value.strip(), since))
    return found


def recent(text: str, days: int = 14, on_date: date | None = None) -> list[Entry]:
    """Строки блока «Сейчас», изменённые за последние N дней."""
    on_date = on_date or date.today()
    fresh: list[Entry] = []
    for entry in entries(text):
        if not entry.since:
            continue
        try:
            when = date(*(int(p) for p in reversed(entry.since.split("."))))
        except (ValueError, TypeError):
            continue
        if 0 <= (on_date - when).days <= days:
            fresh.append(entry)
    return fresh


def state_map(units, days: int = 14) -> list[tuple[str, list[tuple[str, Entry, bool]]]]:
    """Все строки «Сейчас» по базе, по разделам: [(раздел, [(kb_id, строка, изменена ли за `days` дней)])]."""
    grouped: dict[str, list[tuple[str, Entry, bool]]] = {}
    for unit in units:
        rows = entries(unit.text)
        if not rows:
            continue
        fresh = {e.key for e in recent(unit.text, days=days)}
        for entry in rows:
            is_fresh = entry.key in fresh
            grouped.setdefault(unit.section, []).append((unit.id, entry, is_fresh))
    return sorted(grouped.items())


def replace(text: str, key: str, value: str, on_date: str = "") -> tuple[str, str]:
    """Заменяет значение сущности: новое — в «Сейчас», старое — в «Было раньше».

    Возвращает (новый текст единицы, прежнее значение). Прежнее значение пустое,
    если такой сущности ещё не было — тогда строка просто добавляется.
    """
    on_date = on_date or today_ru()
    bounds = _section_bounds(text, HEADING_NOW)
    if bounds is None:
        raise ValueError("в единице нет блока «Сейчас» — заменять нечего")

    wanted = normalize_key(key)
    start, end = bounds
    block = text[start:end]
    lines = block.splitlines()
    new_line = f"- **{key.strip()}:** {value.strip()} — с {on_date}"

    old_value = ""
    old_since = ""
    for index, line in enumerate(lines):
        match = _LINE.match(line.strip())
        if not match or normalize_key(match.group("key")) != wanted:
            continue
        raw_value = match.group("value")
        tail = _SINCE.search(raw_value)
        if tail:
            old_since = tail.group(1)
            raw_value = raw_value[: tail.start()].rstrip()
        old_value = raw_value.strip()
        lines[index] = new_line
        break
    else:
        # Сущности не было — добавляем в конец блока, перед пустой строкой.
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(new_line)

    updated = text[:start] + "\n".join(lines) + "\n\n" + text[end:].lstrip("\n")
    if old_value:
        was_line = f"- **{key.strip()}:** {old_value}"
        if old_since:
            was_line += f" — с {old_since} по {on_date}"
        else:
            was_line += f" — по {on_date}"
        updated = _append_to_was(updated, was_line)
    return updated, old_value


def _append_to_was(text: str, line: str) -> str:
    """Дописывает строку в «Было раньше», заводя блок сразу после «Сейчас», если его ещё нет."""
    bounds = _section_bounds(text, HEADING_WAS)
    if bounds is not None:
        start, end = bounds
        block = text[start:end].rstrip("\n")
        return text[:start] + block + "\n" + line + "\n\n" + text[end:].lstrip("\n")

    now_bounds = _section_bounds(text, HEADING_NOW)
    if now_bounds is None:
        return text + f"\n{HEADING_WAS}\n{line}\n"
    _, now_end = now_bounds
    head = text[:now_end].rstrip("\n")
    tail = text[now_end:].lstrip("\n")
    return f"{head}\n\n{HEADING_WAS}\n{line}\n\n{tail}"


def _section_bounds(text: str, heading: str) -> tuple[int, int] | None:
    """Границы блока: от строки после заголовка до конца списка. None, если раздела нет.

    Заголовок сравнивается по строке целиком. Блок кончается не на следующем `## `,
    а на первой строке, которая не список и не пустая: иначе он захватывал бы
    маркированные строки хронологии из текста единицы.
    """
    pattern = re.compile(rf"^{re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        return None
    start = match.end() + 1
    end = start
    for line in text[start:].splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("- "):
            break
        end += len(line)
    return start, end
