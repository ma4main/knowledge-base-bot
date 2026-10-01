"""Журнал событий: инциденты как строки со сроком, а не как единицы знания.

Считает инциденты, помеченные в базе (`horizon: incident`), и сигналы о сбоях
из потока рабочих чатов; один и тот же сигнал в разные дни склеивается в одно
событие. В базу ничего не пишет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import dedupe

log = logging.getLogger(__name__)

# По каким словам узнаём событие. Намеренно узкий список: «проблема» и «вопрос»
# сюда не входят — иначе журнал соберёт половину рабочей переписки.
SIGNALS = (
    "не работает", "не открывается", "не грузит", "лежит", "недоступ", "упал",
    "упали", "просадка", "просели", "сбой", "отвалил", "не заходит", "не пускает",
    "отчёт не пришёл", "нет доступа",
)

# Слова, при которых сигнал не считается: обсуждают чужое или прошлое, а не сбой.
_NOT_OURS = ("у клиента", "у конкурент", "раньше", "если вдруг")

# Что должно быть сломано, чтобы это было событием отдела: без предмета поломки
# журнал собирал бы половину переписки.
SUBJECTS = (
    "панел", "отчет", "доступ", "оплат", "счет", "договор", "карточк", "позици",
    "заявк", "сервер", "интеграц", "бот", "прокси", "vpn", "впн", "проект",
)

# Признаки того, что проблему уже сняли: это не новое событие, а его конец.
_RESOLVED = ("всё работает", "все работает", "уже работает", "нет критичной", "починили")

# Деньги — не инцидент: работа с дебиторкой, а не сбой.
_MONEY = ("оплат", "счет", "счёт", "дебитор", "должник", "предоплат")


@dataclass
class Event:
    """Событие журнала: о чём, когда впервые, когда последний раз, сколько дней."""
    text: str
    first: str
    last: str
    days: set[str] = field(default_factory=set)
    chats: set[str] = field(default_factory=set)
    who: set[str] = field(default_factory=set)

    @property
    def span(self) -> int:
        return len(self.days)


def is_signal(text: str) -> bool:
    """Похоже ли сообщение на сообщение о сбое: слово про поломку, предмет поломки и нет отсечек."""
    low = (text or "").lower().replace("ё", "е")
    if not any(word in low for word in SIGNALS):
        return False
    if not any(word in low for word in SUBJECTS):
        return False
    if any(word in low for word in _RESOLVED):
        return False
    if any(word in low for word in _MONEY):
        return False
    return not any(word in low for word in _NOT_OURS)


def collect(messages, days: int = 30, today: date | None = None) -> list[Event]:
    """Складывает сообщения-сигналы в события; похожие по смыслу (`dedupe`) — одно событие."""
    today = today or date.today()
    since = today - timedelta(days=days)
    events: dict[str, Event] = {}
    for msg in messages:
        text = (getattr(msg, "text", "") or "").strip()
        if not is_signal(text):
            continue
        at = (getattr(msg, "at", "") or "")[:10]
        try:
            when = date.fromisoformat(at)
        except ValueError:
            continue
        if when < since:
            continue
        hit = dedupe.find_duplicate(text, {k: e.text for k, e in events.items()})
        key = hit[0] if hit else text[:80]
        event = events.get(key)
        if event is None:
            event = events[key] = Event(text=text[:160], first=at, last=at)
        event.days.add(at)
        event.first = min(event.first, at)
        event.last = max(event.last, at)
        chat = getattr(msg, "topic", "") or getattr(msg, "chat_title", "")
        if chat:
            event.chats.add(chat)
        who = getattr(msg, "user_name", "")
        if who:
            event.who.add(who)
    # Сначала долгие: событие, о котором говорили три дня, важнее разовой реплики.
    return sorted(events.values(), key=lambda e: (-e.span, e.last), reverse=False)


def render(events: list[Event], incidents, days: int) -> str:
    """Отчёт для человека: что случилось за период и сколько это длилось."""
    out = [f"📋 <b>Журнал событий за {days} дней</b>", ""]
    if incidents:
        out.append("<b>Разобранные инциденты (есть в базе):</b>")
        for unit in incidents:
            when = unit.happened or unit.updated if hasattr(unit, "updated") else unit.happened
            out.append(f"• {unit.title[:90]}" + (f" — {when}" if when else ""))
        out.append("")

    if events:
        long_ones = [e for e in events if e.span > 1]
        out.append(f"<b>Замечено в чатах: {len(events)} событий</b>")
        if long_ones:
            out.append(f"<i>из них тянулись больше дня: {len(long_ones)}</i>")
        out.append("")
        for event in events[:12]:
            span = (
                f"{event.span} дн. ({event.first[5:]}–{event.last[5:]})"
                if event.span > 1 else event.first[5:]
            )
            where = ", ".join(sorted(event.chats)[:2])
            out.append(f"• <b>{span}</b> · {event.text[:120]}" + (f"\n   <i>{where}</i>" if where else ""))
        if len(events) > 12:
            out.append(f"• …и ещё {len(events) - 12}")
    else:
        out.append("В чатах о сбоях не говорили — событий нет.")

    out.append("")
    out.append(
        "<i>Журнал считается из потока чатов и помеченных инцидентов базы. "
        "В базу он ничего не пишет: инцидент — это строка события, а не правило.</i>"
    )
    return "\n".join(out)
