"""Критичные сигналы руководителям: бот пишет человеку только когда без него никак.

Виды: llm — модель не отвечает; git — правки базы не уезжают в хранилище;
disk — на сервере кончается место. Сигнал уходит после серии сбоев подряд,
не чаще раза в сутки на вид; когда починилось — одно «снова работает».
Счётчики живут в памяти процесса, отметка «уже писали сегодня» — в состоянии бота.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import date
from pathlib import Path

from formatting import esc_html as _esc

log = logging.getLogger(__name__)

LLM, GIT, DISK = "llm", "git", "disk"

# Сколько сбоев подряд считаем проблемой, а не икотой.
THRESHOLD = {LLM: 3, GIT: 3, DISK: 1}
# Отказ оплаты — не икота: OpenRouter отвечает 402, когда деньги кончились.
PAYMENT_MARKERS = ("вернул 402", "insufficient credits", "requires more credits")

# Как часто проверяем, не пора ли кому-то написать, и меряем диск.
CHECK_EVERY = 300
DISK_MIN_FREE_GB = 2.0
DISK_MIN_FREE_SHARE = 0.10

TEXTS = {
    LLM: (
        "🔴 <b>Не могу достучаться до модели</b>\n\n"
        "Сбоев подряд: {count}. Пока так, на вопросы менеджеров я отвечаю «что-то "
        "сломалось», а в базу ничего не записываю.\n\n"
        "Чаще всего причина одна из двух:\n"
        "• закончились деньги на OpenRouter — пополнить баланс;\n"
        "• модель сняли с обслуживания — Меню → Управление → Бот → Модель, выбрать другую.\n\n"
        "<i>Последний ответ сервиса: {detail}</i>"
    ),
    GIT: (
        "🟠 <b>Правки базы не уезжают в хранилище (git)</b>\n\n"
        "Сбоев подряд: {count}. Сам я работаю и отвечаю как обычно, правки лежат на "
        "сервере. Но их резервной копии нет: если с сервером что-то случится, они "
        "пропадут.\n\n"
        "Нужен техник — раздел «Правки не уезжают в git» в docs/OPERATIONS.md.\n\n"
        "<i>Что ответил git: {detail}</i>"
    ),
    DISK: (
        "🟠 <b>На сервере заканчивается место</b>\n\n"
        "{detail}. Когда место кончится, я не смогу записывать ни базу, ни поток чатов.\n\n"
        "Нужен техник — раздел «Заканчивается место» в docs/OPERATIONS.md (обычно помогает одна "
        "команда чистки старых сборок Docker)."
    ),
}
RECOVERED = {
    LLM: "🟢 Модель снова отвечает — работаю как обычно.",
    GIT: "🟢 Правки базы снова уезжают в хранилище (git).",
    DISK: "🟢 Места на сервере снова достаточно.",
}

_fails: dict[str, int] = {}
_detail: dict[str, str] = {}
_raised: set[str] = set()  # по каким видам сигнал уже ушёл и ждёт «снова работает»


def fail(kind: str, detail: str = "") -> None:
    """Зафиксировать сбой. Звать из места сбоя; рассылкой занимается `run`."""
    _fails[kind] = _fails.get(kind, 0) + 1
    _detail[kind] = " ".join((detail or "").split())[:300]
    if kind == LLM and any(m in _detail[kind].lower() for m in PAYMENT_MARKERS):
        # Деньги кончились — ждать серии незачем, сама она не пройдёт.
        _fails[kind] = max(_fails[kind], THRESHOLD[LLM])


def ok(kind: str) -> None:
    """Зафиксировать успех: серия сбоев оборвалась."""
    _fails[kind] = 0


def due(state) -> list[tuple[str, str]]:
    """Что пора отправить: список (вид, текст). Чистая логика — проверяется без Telegram."""
    out: list[tuple[str, str]] = []
    today = date.today().isoformat()
    for kind, limit in THRESHOLD.items():
        count = _fails.get(kind, 0)
        if count >= limit:
            if state.alert_sent_on(kind) != today:
                out.append((kind, TEXTS[kind].format(count=count, detail=_esc(_detail.get(kind, "")))))
                state.mark_alert(kind, today)
            _raised.add(kind)
        elif kind in _raised:
            _raised.discard(kind)
            out.append((kind, RECOVERED[kind]))
    return out


def check_disk(path: Path) -> None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return
    free_gb = usage.free / 1024 ** 3
    if free_gb < DISK_MIN_FREE_GB or usage.free / usage.total < DISK_MIN_FREE_SHARE:
        fail(DISK, f"Свободно {free_gb:.1f} ГБ из {usage.total / 1024 ** 3:.0f}")
    else:
        ok(DISK)


async def run(bot, config, state, notify, every: int = CHECK_EVERY) -> None:
    """Фоновая задача: раз в несколько минут меряет диск и рассылает то, что пора."""
    while True:
        try:
            check_disk(config.data_dir)
            for kind, text in due(state):
                sent = await notify(bot, config, text, None)
                log.warning("Сигнал руководителям «%s»: доставлено %s", kind, sent)
        except Exception:
            log.exception("Сбой в рассылке критичных сигналов")
        await asyncio.sleep(every)


def reset() -> None:
    """Для проверок: вернуть модуль в исходное состояние."""
    _fails.clear()
    _detail.clear()
    _raised.clear()

