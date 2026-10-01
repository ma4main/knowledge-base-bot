"""Единый разбор Markdown-шапки (frontmatter) ядра.

Ядро — переносимый Markdown; его шапку должен парсить ровно один код,
иначе копии в kb.py и files_lib.py разъезжаются. Плоский разбор: строковые
поля и списки в [скобках]; вложенные блоки (sources) для наших задач не нужны.
"""

from __future__ import annotations


def parse_frontmatter(text: str, with_body: bool = False) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue  # вложенные блоки и строки без ключа пропускаем
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("\"'")
    if with_body:
        out["__body__"] = text[end + 4 :].strip()
    return out
