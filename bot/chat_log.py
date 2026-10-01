"""Слушатель рабочих чатов: поток сообщений на диск.

Пишем всё, куда бота добавили, пока чат не заглушили; поток лежит отдельным слоем
репозитория `chats-live/` (JSONL по чату и месяцу); файлы не скачиваем — только пометка.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Верхний уровень репозитория, не внутри knowledge/: это сырьё.
REL_DIR = "chats-live"

# Ограничение на длину записи: пересланные простыни раздувают файл.
MAX_TEXT = 4000

# Ники служебных ботов, чьи сообщения не попадают в текст потока для модели (поток их записывает).
IGNORE_SENDERS: set[str] = set()


@dataclass(frozen=True)
class ChatMessage:
    at: str
    chat_id: int
    chat_title: str
    user_id: int
    user_name: str
    username: str
    message_id: int
    text: str
    kind: str  # text | photo | document | voice | video | sticker | reaction | other
    # Топик форума; 0 — общий поток чата.
    thread_id: int = 0
    topic: str = ""

    def as_line(self) -> str:
        who = f"@{self.username}" if self.username else self.user_name
        mark = "" if self.kind == "text" else f" [{self.kind}]"
        where = f" ({self.topic})" if self.topic else ""
        # Номер сообщения — доказуемая ссылка на первоисточник факта.
        num = f" #{self.message_id}" if self.message_id else ""
        return f"[{self.at[:16]}{num}]{where} {who}{mark}: {self.text}"


class ChatLog:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: int, when: datetime) -> Path:
        folder = self.root / str(chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{when:%Y-%m}.jsonl"

    def record(self, entry: ChatMessage) -> None:
        """Дописывает сообщение в поток чата; ошибка записи не роняет бота."""
        try:
            when = datetime.now(timezone.utc)
            line = json.dumps(_trim(entry), ensure_ascii=False)
            with self._path(entry.chat_id, when).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            log.warning("Не смог записать сообщение чата %s", entry.chat_id, exc_info=True)

    def messages(
        self, chat_id: int, limit: int = 400, since: str | None = None,
        until: str | None = None,
    ) -> list[ChatMessage]:
        """Последние сообщения чата. `since` — только новее указанного ISO-времени, `until` — не свежее указанного."""
        folder = self.root / str(chat_id)
        if not folder.is_dir():
            return []
        out: list[ChatMessage] = []
        # Идём от свежих файлов к старым и останавливаемся, когда набрали лимит.
        for path in sorted(folder.glob("*.jsonl"), reverse=True):
            rows = _read(path)
            out = rows + out
            if len(out) >= limit and since is None:
                break
        if since:
            out = [m for m in out if m.at > since]
        if until:
            out = [m for m in out if m.at <= until]
        return out[-limit:]

    def recent_all(self, days: int = 30, limit_per_chat: int = 2000) -> list[ChatMessage]:
        """Сообщения всех записываемых чатов за период — сырьё для журнала событий."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        out: list[ChatMessage] = []
        if not self.root.is_dir():
            return out
        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue
            try:
                chat_id = int(folder.name)
            except ValueError:
                continue
            out.extend(self.messages(chat_id, limit=limit_per_chat, since=since))
        return sorted(out, key=lambda m: m.at)

    def context(
        self,
        chat_id: int,
        thread_id: int = 0,
        limit: int = 10,
        skip_message_id: int | None = None,
    ) -> list[ChatMessage]:
        """Последние сообщения одного топика — контекст для ответа в рабочем чате; `skip_message_id` — сам вопрос."""
        if limit <= 0:
            return []
        # Читаем с запасом: на один топик приходится много сообщений из других.
        rows = self.messages(chat_id, limit=max(limit * 20, 200))
        same_topic = [
            m for m in rows
            if m.thread_id == thread_id and m.message_id != skip_message_id
        ]
        return same_topic[-limit:]

    def counts(self) -> dict[int, int]:
        """Сколько записано по каждому чату — для /chats."""
        result: dict[int, int] = {}
        for folder in self.root.iterdir() if self.root.is_dir() else []:
            if not folder.is_dir():
                continue
            try:
                chat_id = int(folder.name)
            except ValueError:
                continue
            total = 0
            for path in folder.glob("*.jsonl"):
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        total += sum(1 for _ in fh)
                except OSError:
                    continue
            result[chat_id] = total
        return result

    def as_text(
        self, chat_id: int, limit: int = 400, since: str | None = None,
        until: str | None = None,
    ) -> str:
        """Поток чата в читаемом виде, сгруппированный по топикам — то, что уходит в модель на выжимку."""
        messages = [
            m for m in self.messages(chat_id, limit, since=since, until=until)
            if m.username not in IGNORE_SENDERS
        ]
        if not messages:
            return ""
        by_topic: dict[str, list[ChatMessage]] = {}
        for message in messages:
            key = message.topic or (f"топик #{message.thread_id}" if message.thread_id else "")
            by_topic.setdefault(key, []).append(message)
        if len(by_topic) == 1 and "" in by_topic:
            return "\n".join(m.as_line() for m in messages)
        parts = []
        for topic, rows in by_topic.items():
            head = f"--- Топик: {topic} ---" if topic else "--- Общий поток чата ---"
            parts.append(head + "\n" + "\n".join(m.as_line() for m in rows))
        return "\n\n".join(parts)

    def find(self, chat_id: int, message_id: int) -> ChatMessage | None:
        """Сообщение по номеру — кто сказал и когда. None — не нашли."""
        if not message_id:
            return None
        # Запись о реакции несёт message_id сообщения, на которое реагировали.
        for m in reversed(self.messages(chat_id, limit=3000)):
            if m.message_id == message_id and m.kind != "reaction":
                return m
        return None

    def topics(self, chat_id: int) -> dict[int, str]:
        """Какие топики встречались в потоке чата — для сводки по топикам."""
        found: dict[int, str] = {}
        for message in self.messages(chat_id, limit=2000):
            if message.thread_id and message.topic:
                found[message.thread_id] = message.topic
        return found


def _trim(entry: ChatMessage) -> dict:
    data = entry.__dict__.copy()
    data["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = (data.get("text") or "")[:MAX_TEXT]
    data["text"] = text
    return data


def _read(path: Path) -> list[ChatMessage]:
    rows: list[ChatMessage] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                rows.append(ChatMessage(**json.loads(raw)))
            except (json.JSONDecodeError, TypeError):
                continue  # битая строка не должна ломать чтение всего потока
    except OSError:
        log.warning("Не смог прочитать поток %s", path, exc_info=True)
    return rows


def topic_of(message) -> tuple[int, str]:
    """Топик сообщения: (thread_id, название, если удалось узнать); вне топиков thread_id = 0."""
    if not getattr(message, "is_topic_message", False):
        return 0, ""
    thread_id = message.message_thread_id or 0
    created = getattr(message, "forum_topic_created", None)
    if created is not None and getattr(created, "name", ""):
        return thread_id, created.name
    # Первое сообщение в топике отвечает на служебное «топик создан» — там есть имя.
    reply = getattr(message, "reply_to_message", None)
    reply_created = getattr(reply, "forum_topic_created", None) if reply else None
    if reply_created is not None and getattr(reply_created, "name", ""):
        return thread_id, reply_created.name
    return thread_id, ""


def describe_kind(message) -> tuple[str, str]:
    """Что за сообщение и какой у него текст. Файлы не скачиваем — только пометка."""
    if message.text:
        return "text", message.text
    caption = message.caption or ""
    if message.photo:
        return "photo", caption or "(фото)"
    if message.document:
        name = getattr(message.document, "file_name", "") or "файл"
        return "document", caption or f"(документ: {name})"
    if message.voice:
        return "voice", "(голосовое)"
    if message.video or message.video_note:
        return "video", caption or "(видео)"
    if message.sticker:
        # Стикер часто и есть весь ответ («ок», «жду») — эмодзи сохраняем как смысл.
        emoji = getattr(message.sticker, "emoji", "") or ""
        return "sticker", f"(стикер {emoji})".strip().replace("( ", "(")
    return "other", caption or "(вложение)"
