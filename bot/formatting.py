"""Приведение ответа модели к тому, что Telegram готов показать.

Модель пишет обычным Markdown. Родной парсер Telegram на нём регулярно падает
(незакрытая `*`, символы из текста базы) — и вместо ответа менеджер видит ошибку.
Поэтому переводим сами в HTML: экранируем всё, потом возвращаем разметку.
"""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
# Запас под to_html: экранирование и теги раздувают кусок, а лимит Telegram — 4096.
CHUNK_SIZE = 3400


def esc_html(text: str) -> str:
    """Экранирование без разметки Markdown — для служебных строк, где `*` и `` ` `` должны остаться собой."""
    return html.escape(text or "", quote=False)


def to_html(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"```(?:\w+)?\n(.+?)```", r"<pre>\1</pre>", out, flags=re.DOTALL)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", out)
    # Заголовки Markdown Telegram не понимает — превращаем в жирную строку.
    out = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.MULTILINE)
    return out


def split_message(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """Режет длинный ответ по границам абзацев, затем строк, затем жёстко."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
