"""Транслитерация в имена файлов — одна на весь бот.

Таблица повторяет соглашение, сложившееся в именах файлов базы (ё → yo, ц → c),
чтобы новые файлы назывались так же, как соседние.
"""

from __future__ import annotations

import re

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slugify(text: str, max_len: int = 40, fallback: str = "file") -> str:
    """Латиница, цифры и дефисы — безопасное имя файла в git на любой ОС; обрезка по границе слова."""
    out = "".join(_TRANSLIT.get(char, char) for char in text.lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    if len(out) > max_len:
        out = out[:max_len].rsplit("-", 1)[0] or out[:max_len]
    return out or fallback
