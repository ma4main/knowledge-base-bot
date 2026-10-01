"""Агентский цикл: вопрос → тип запроса → выбор единиц по каталогу → загрузка → ответ.

Тип запроса определяется отдельным дешёвым вызовом (`qtype.Classifier`) и задаёт формат ответа.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import factcheck
import qtype
from files_lib import FileEntry
from llm.base import Completion, LLMClient, Usage
from prompts import build_system_blocks
from tools import TOOL_SCHEMAS, ToolRunner

log = logging.getLogger(__name__)

HISTORY_LIMIT = 12

# Типы вопросов, ответ на которые обязан стоять на тексте единиц.
_NEEDS_SOURCES = (qtype.FACT, qtype.SITUATION, qtype.OBJECTION)

# Как звучит честное «в базе этого нет».
_GAP_PHRASES = (
    "в базе этого нет", "в базе нет", "базе не нашёл", "базе не нашел",
    "нет в базе", "не нашёл в базе", "не нашел в базе", "этого у меня нет",
)


def _admits_gap(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")
    return any(phrase.replace("ё", "е") in low for phrase in _GAP_PHRASES)

# Сколько текста единицы кладём в запрос «Подробнее».
DETAIL_UNIT_CHARS = 6000


@dataclass
class Answer:
    text: str
    usage: Usage = field(default_factory=Usage)
    files: list[FileEntry] = field(default_factory=list)
    # Единицы, которые модель реально загрузила: нужны кэшу ответов и кнопке «Подробнее».
    units: list[str] = field(default_factory=list)
    kind: str = qtype.OTHER


@dataclass
class Dialog:
    """История одного менеджера; живёт в памяти процесса, перезапуск её обнуляет."""

    messages: list[dict] = field(default_factory=list)
    # Тип первого вопроса разговора; продолжение заново не классифицируем.
    kind: str = ""

    def add(self, message: dict) -> None:
        self.messages.append(message)
        if len(self.messages) > HISTORY_LIMIT:
            # Не оставляем ответов инструментов без вызвавшего их сообщения.
            cut = len(self.messages) - HISTORY_LIMIT
            while cut < len(self.messages) and self.messages[cut].get("role") == "tool":
                cut += 1
            self.messages = self.messages[cut:]

    def clear(self) -> None:
        self.messages.clear()
        self.kind = ""


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRunner,
        index_text: str,
        cache_ttl: str = "1h",
        max_iterations: int = 6,
        kb=None,
        links=None,
        classifier: "qtype.Classifier | None" = None,
        people=None,
        audit=None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self._cache_ttl = cache_ttl
        self.classifier = classifier
        self.audit = audit
        # kb/links/people — живые объекты, чтобы префикс пересобирался после правок базы;
        # index_text — запасной путь для тестов.
        self._kb = kb
        self._links = links
        self._people = people
        self._fallback_index = index_text
        self._prefix_key: tuple[str, str, str] | None = None
        self._blocks: list[dict] = []

    @property
    def system_blocks(self) -> list[dict]:
        """Системный префикс; пересобирается только когда каталог, ссылки или справочник людей изменились."""
        index_text = self._kb.index_text if self._kb is not None else self._fallback_index
        links_text = self._links.prompt_text() if self._links is not None else ""
        people_text = self._people.prompt_text() if self._people is not None else ""
        key = (index_text, links_text, people_text)
        if key != self._prefix_key:
            self._prefix_key = key
            self._blocks = build_system_blocks(
                index_text, self._cache_ttl, links_text, people_text
            )
            log.info("Системный префикс пересобран (каталог/ссылки/люди изменились)")
        return self._blocks

    async def classify_intent(self, text: str):
        """Намерение обращения и расход; без классификатора всё считается вопросом."""
        if self.classifier is None:
            return qtype.ASK, Usage()
        return await self.classifier.intent(text)

    async def answer(
        self,
        dialog: Dialog,
        question: str,
        model: str | None = None,
        in_group: bool = False,
        context: str = "",
    ) -> Answer:
        """`in_group` — вопрос из рабочего чата (формат короче); `context` — последние
        сообщения топика, уже обёрнутые вызывающим в границы данных."""
        total = Usage()

        # Тип определяем один раз на разговор; классификатору отдаём вопрос вместе
        # с контекстом топика, иначе обрывок фразы классифицируется как ПРОЧЕЕ.
        kind = dialog.kind
        if not kind:
            if self.classifier is not None:
                kind, class_usage = await self.classifier.classify(
                    f"{context}\n\nВОПРОС: {question}" if context else question
                )
                _accumulate(total, class_usage)
            else:
                kind = qtype.OTHER
            dialog.kind = kind

        # Дата и формат ответа идут в сообщение пользователя, а не в кэшируемый префикс;
        # контекст — перед вопросом, чтобы модель отвечала на вопрос, а не на переписку.
        dialog.add(
            {
                "role": "user",
                "content": (
                    f"[сегодня {date.today().isoformat()}]\n"
                    + (f"{context}\n\n" if context else "")
                    + f"ВОПРОС: {question}\n\n"
                    + qtype.format_block(kind, in_group=in_group)
                ),
            }
        )

        text, usage, files, units = await self._loop(dialog, None, model)
        _accumulate(total, usage)

        # Ответ на предметный вопрос без единиц — ответ по памяти модели, а не по базе:
        # один раз возвращаем её к базе, второй раз не настаиваем.
        if kind in _NEEDS_SOURCES and not units and not _admits_gap(text):
            log.warning("Ответ без единиц на вопрос типа %s — переспрашиваю по базе", kind)
            dialog.add({
                "role": "user",
                "content": (
                    "Ты ответил, не открыв ни одной единицы базы. Так нельзя: ответ "
                    "должен стоять на её тексте, а не на твоей памяти. Найди подходящие "
                    "единицы по каталогу, загрузи их через get_kb_units и ответь по ним. "
                    "Если подходящей единицы в каталоге нет — так и скажи: «в базе этого нет»."
                ),
            })
            retry_text, retry_usage, retry_files, retry_units = await self._loop(dialog, None, model)
            _accumulate(total, retry_usage)
            if retry_units or _admits_gap(retry_text):
                text, files, units = retry_text, files + retry_files, retry_units

        # Фраза для клиента уходит наружу дословно — сверяем её с источником отдельным вызовом.
        if kind == qtype.OBJECTION and self.audit is not None:
            phrase = factcheck.client_phrase(text)
            if phrase:
                quotes, audit_usage = await self.audit.check(phrase, self._units_text(units))
                _accumulate(total, audit_usage)
                if quotes == [factcheck.CHECK_FAILED]:
                    # Сверка не отработала — говорим об этом, иначе фразу сочтут проверенной.
                    text = f"{text}\n\n{factcheck.failed_text()}"
                elif quotes:
                    text = f"{text}\n\n{factcheck.warning_text(quotes)}"

        return Answer(text=text, usage=total, files=files, units=units, kind=kind)

    def _units_text(self, units: list[str]) -> str:
        """Текст единиц, на которых стоит ответ — для сверки фразы с источником."""
        if not units or self.tools is None:
            return ""
        found, _missing = self.tools.kb.get_many(units)
        return "\n\n".join(f"===== {u.id} · {u.title} =====\n{u.text}" for u in found)

    async def expand(
        self,
        question: str,
        units: list[str],
        model: str | None = None,
        in_group: bool = False,
    ) -> Answer:
        """«Подробнее»: разворачивает короткий ответ, подкладывая в запрос текст уже выбранных единиц."""
        found, _missing = self.tools.kb.get_many(units)
        blocks = [
            f"===== {unit.id} · {unit.title} =====\n{unit.text[:DETAIL_UNIT_CHARS]}"
            for unit in found
        ]
        loaded = "\n\n".join(blocks) if blocks else "(единицы не сохранились — найди их сам по каталогу)"
        messages = [
            {
                "role": "user",
                "content": (
                    f"[сегодня {date.today().isoformat()}]\n{question}\n\n"
                    f"# ЕДИНИЦЫ БАЗЫ, НА КОТОРЫХ СТОЯЛ КОРОТКИЙ ОТВЕТ\n\n{loaded}\n\n"
                    + qtype.format_block(qtype.OTHER, in_group=in_group, detail=True)
                ),
            }
        ]
        text, usage, files, used = await self._loop(None, messages, model)
        log.info("«Подробнее» по %s: %d символов", ", ".join(units) or "—", len(text))
        return Answer(
            text=text, usage=usage, files=files,
            units=sorted(set(units) | set(used)), kind=qtype.OTHER,
        )

    async def _loop(
        self, dialog: Dialog | None, scratch: list[dict] | None, model: str | None
    ) -> tuple[str, Usage, list[FileEntry], list[str]]:
        """Круг «модель → инструменты → модель»; `dialog` не None — пишем в него, иначе в `scratch`."""
        total = Usage()
        files: list[FileEntry] = []  # файлы, которые модель попросила отправить
        units: list[str] = []  # единицы, реально загруженные из базы

        # Список берём заново на каждом шаге: `Dialog.add` при обрезке истории заменяет его.
        def current() -> list[dict]:
            return dialog.messages if dialog is not None else (scratch or [])

        def add(message: dict) -> None:
            if dialog is not None:
                dialog.add(message)
            else:
                current().append(message)

        for step in range(self.max_iterations):
            completion = await self.llm.complete(
                self.system_blocks, current(), TOOL_SCHEMAS, model=model
            )
            _accumulate(total, completion.usage)
            add(_assistant_message(completion))

            if not completion.tool_calls:
                text = completion.text.strip()
                return (
                    text or "Не смог сформулировать ответ. Переспроси, пожалуйста.",
                    total,
                    files,
                    units,
                )

            for call in completion.tool_calls:
                result = self.tools.run(call.name, call.arguments, files, units)
                add({"role": "tool", "tool_call_id": call.id, "content": result})
            log.debug("Шаг %d: выполнено инструментов — %d", step + 1, len(completion.tool_calls))

        log.warning("Достигнут предел в %d шагов, ответа нет", self.max_iterations)
        return (
            "Запутался в поиске по базе и не собрал ответ. "
            "Попробуй сформулировать вопрос конкретнее.",
            total,
            files,
            units,
        )


def _assistant_message(completion: Completion) -> dict:
    """Сообщение ассистента в том виде, в каком его ждёт API на следующем шаге."""
    message: dict = {"role": "assistant", "content": completion.text or None}
    raw_calls = completion.raw_message.get("tool_calls")
    if raw_calls:
        message["tool_calls"] = raw_calls
    return message


def _accumulate(total: Usage, step: Usage) -> None:
    total.prompt_tokens += step.prompt_tokens
    total.completion_tokens += step.completion_tokens
    total.cached_tokens += step.cached_tokens
    total.cache_write_tokens += step.cache_write_tokens
    total.cost += step.cost
    total.cache_discount += step.cache_discount
