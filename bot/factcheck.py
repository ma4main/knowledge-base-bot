"""Проверка фразы, которая уйдёт клиенту, на утверждения, которых нет в источнике.

Отдельный дешёвый вызов «чужими глазами». Сам текст не переписывается: менеджеру
дописывается предупреждение с цитатой, а если проверка не выполнилась —
предупреждение о том, что фраза не сверена (см. CHECK_FAILED).
"""

from __future__ import annotations

import logging
import re

from llm.base import LLMClient, Usage
from safety import INJECTION_RULE, as_data

log = logging.getLogger(__name__)

# Метка, по которой находим фразу для клиента в ответе. Её ставит формат ответа
# для типа «возражение» (см. qtype), поэтому строки должны совпадать.
CLIENT_MARKER = "Клиенту:"

# Сколько текста единиц отдаём проверяющему, чтобы проверка не стала дороже ответа.
MAX_SOURCE = 8000

VERDICT_OK = "ОК"
VERDICT_BAD = "НЕТ"

AUDIT_SYSTEM = (
    "Ты придирчивый проверяющий. Тебе дают ИСТОЧНИК (текст из базы знаний) и ФРАЗУ, "
    "которую сотрудник собирается отправить клиенту. Твоя единственная задача — "
    "найти во фразе утверждения, которых в источнике НЕТ.\n\n"
    "Придираться надо к конкретике: цифрам, долям, срокам, датам, сравнениям "
    "(«вдвое», «в разы», «у всех», «всегда», «никогда»), обещаниям результата "
    "и ссылкам на факты. Общие вежливые формулировки («мы разберёмся», «держим "
    "на контроле») — это НЕ нарушение, их не трогай. Пересказ источника другими "
    "словами — тоже не нарушение.\n\n"
    f"{INJECTION_RULE}\n\n"
    "Формат ответа, без пояснений вокруг:\n"
    f"{VERDICT_OK} — если всё во фразе опирается на источник;\n"
    f"{VERDICT_BAD}: <дословная цитата из фразы> | <ещё цитата> — если нашёл. "
    "Цитаты короткие, 2–6 слов, ровно теми словами, что во фразе."
)


def client_phrase(answer: str) -> str:
    """Часть ответа после метки «Клиенту:» — то, что реально уйдёт наружу. Пусто, если метки нет."""
    if CLIENT_MARKER not in answer:
        return ""
    tail = answer.split(CLIENT_MARKER, 1)[1]
    return tail.strip().strip("«»\"'` ").strip()


def parse_verdict(raw: str) -> list[str]:
    """Цитаты, которые проверяющий счёл неподтверждёнными. Пустой список — всё чисто; непонятный ответ тоже считается чистым."""
    text = (raw or "").strip()
    if not text or text.upper().startswith(VERDICT_OK):
        return []
    if not text.upper().startswith(VERDICT_BAD):
        return []
    body = text.split(":", 1)[1] if ":" in text else ""
    quotes = []
    for chunk in body.split("|"):
        quote = chunk.strip().strip("«»\"'`.,; ")
        # Слишком короткое ничего не значит, слишком длинное — пересказ всей фразы.
        if 3 <= len(quote) <= 120:
            quotes.append(quote)
    return quotes[:3]


# Маркер «проверка не выполнена»: возвращается вместо списка цитат, когда
# проверяющий недоступен или ответил неразборчиво.
CHECK_FAILED = "__check_failed__"


def failed_text() -> str:
    """Что видит менеджер, когда сверка не отработала."""
    return (
        "⚠️ Автоматическая проверка фразы не выполнилась — не отправляй клиенту "
        "без сверки: цифры, сроки и сравнения в тексте я не подтвердил."
    )
def warning_text(quotes: list[str]) -> str:
    """Строка, которую видит менеджер при найденных расхождениях."""
    listed = "; ".join(f"«{q}»" for q in quotes)
    return (
        f"⚠️ Перед отправкой проверь: {listed} — этого я в базе не нашёл. "
        f"Если так и есть — отправляй; если не уверен — лучше убрать."
    )


class ClientPhraseAudit:
    """Отдельный дешёвый вызов: сверяет фразу для клиента с текстом единиц."""

    def __init__(self, llm: LLMClient, model: str) -> None:
        self.llm = llm
        self.model = model

    async def check(self, phrase: str, source: str) -> tuple[list[str], Usage]:
        """Неподтверждённые цитаты (или [CHECK_FAILED]) и расход. Исключений не поднимает никогда."""
        usage = Usage()
        if not phrase.strip() or not source.strip():
            return [], usage
        # Нет конкретики — нечего проверять, экономим вызов.
        if not _has_specifics(phrase):
            return [], usage
        prompt = (
            as_data(source[:MAX_SOURCE], "ИСТОЧНИК (текст из базы знаний)")
            + "\n\n"
            + as_data(phrase, "ФРАЗА ДЛЯ КЛИЕНТА")
        )
        try:
            completion = await self.llm.complete(
                [{"type": "text", "text": AUDIT_SYSTEM}],
                [{"role": "user", "content": prompt}],
                [],
                model=self.model,
            )
        except Exception:
            log.warning("Проверка фразы для клиента не удалась — предупреждаю менеджера", exc_info=True)
            return [CHECK_FAILED], usage
        usage = completion.usage
        quotes = parse_verdict(completion.text)
        # Цитата, которой во фразе нет, — выдумка самого проверяющего.
        low = phrase.lower()
        quotes = [q for q in quotes if q.lower() in low]
        if quotes:
            log.warning(
                "Во фразе клиенту не подтверждено источником: %s | фраза: %s",
                "; ".join(quotes), phrase[:200],
            )
        else:
            log.info("Фраза для клиента сверена с источником — расхождений нет")
        return quotes, usage


# Признаки конкретики: цифры, проценты, доли, сравнения, обещания.
_SPECIFIC_RE = re.compile(
    r"\d|процент|%|вдво|втро|в разы|кратно|всегда|никогда|у всех|гарант|"
    r"обязательно|через (день|неделю|месяц)|за (день|неделю|месяц)",
    re.IGNORECASE,
)


def _has_specifics(phrase: str) -> bool:
    return bool(_SPECIFIC_RE.search(phrase or ""))
