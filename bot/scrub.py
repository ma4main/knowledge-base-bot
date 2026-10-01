"""Вычистка клиентских данных из потока чатов перед отправкой в модель.

Кодом, без модели, вычищаются телефоны в российских форматах, почтовые адреса
и ссылки/домены вне белого списка своих и продуктовых. Имена клиентов не вычищаются:
справочника клиентов нет, регэкспом их не отличить. Вычищенное заменяется пометкой
в квадратных скобках, чтобы модель не достраивала пропуск догадкой.
"""

from __future__ import annotations

import re

# Белый список своих и продуктовых доменов (поддомены накрываются корнем); пусто — скрываются все ссылки.
KEEP_DOMAINS: frozenset[str] = frozenset()

# +7 или 8, затем десять цифр в любой разбивке пробелами/скобками/дефисами.
# Границы (?<!\d)/(?!\d) отсекают длинные числа, а требование ровно десяти цифр
# после кода — цены («8 900 рублей» не телефон: дальше нет ещё семи цифр).
_PHONE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s\-()]{0,3}\d{3}[\s\-()]{0,3}\d{3}[\s\-()]{0,2}\d{2}[\s\-()]{0,2}\d{2}(?!\d)"
)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")

# Ссылка с протоколом — целиком; голый домен — по типовым зонам. Кириллические
# домены (.рф) сюда же. Хвост пути захватываем: в нём бывают имена и id.
_URL = re.compile(
    r"https?://\S+"
    r"|(?<![\w@./-])(?:[\w-]+\.)+(?:ru|com|net|org|su|io|biz|info|me|us|рф)(?:/\S*)?",
    re.IGNORECASE,
)


def _domain(match: str) -> str:
    host = re.sub(r"^https?://", "", match, flags=re.IGNORECASE)
    return host.split("/")[0].split("?")[0].lower().strip(".")


def _keep(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in KEEP_DOMAINS)


def clean(text: str) -> tuple[str, int]:
    """Возвращает (вычищенный текст, сколько фрагментов скрыто)."""
    hidden = 0

    def _sub(pattern: re.Pattern, repl, s: str) -> str:
        def _one(m: re.Match) -> str:
            nonlocal hidden
            out = repl(m) if callable(repl) else repl
            if out != m.group(0):
                hidden += 1
            return out

        return pattern.sub(_one, s)

    def _url_repl(m: re.Match) -> str:
        return m.group(0) if _keep(_domain(m.group(0))) else "[ссылка скрыта]"

    text = _sub(_PHONE, "[телефон скрыт]", text)
    text = _sub(_EMAIL, "[почта скрыта]", text)
    text = _sub(_URL, _url_repl, text)
    return text, hidden
