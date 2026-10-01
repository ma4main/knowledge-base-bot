"""Отметка живости: доказательство, что бот не просто запущен, а работает.

Docker видит только смерть процесса, а зависший long polling снаружи неотличим
от тихого дня в чатах. Поэтому отметка ставится после успешного `get_me()`: свежий
файл означает «процесс жив и Telegram отвечает», протухший — контейнер нездоров.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import atomic
import botstate

log = logging.getLogger(__name__)

FILE_NAME = "heartbeat"

# Как часто отмечаемся: реже таймаута проверки, но без лишнего шума запросами к Telegram.
EVERY = 60

# Насколько старой может быть отметка, чтобы контейнер ещё считался здоровым:
# переживает одну-две неудачные попытки подряд (ночные Bad Gateway лечатся сами).
STALE_AFTER = 180

# Через сколько секунд непрерывного отказа бот перезапускает сам себя.
RESTART_AFTER = 900


def path_for(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


async def run(bot, data_dir: Path, every: int = EVERY) -> None:
    """Фоновая задача: дёргает Telegram и обновляет отметку."""
    target = path_for(data_dir)
    last_ok = time.time()
    while True:
        try:
            await bot.get_me()
            atomic.write_text(target, str(int(time.time())))
            last_ok = time.time()
        except Exception:
            # Без стека: на сетевую икоту он ни к чему, долгий отказ увидит healthcheck.
            log.warning("Отметка живости не обновилась — Telegram не ответил")
            # Docker перезапускает контейнер только при смерти процесса, «нездоров» —
            # лишь надпись в `docker ps`. Поэтому долгий отказ лечим сами: выходим
            # с ошибкой, и `restart: unless-stopped` поднимает бота с чистыми соединениями.
            if time.time() - last_ok > RESTART_AFTER:
                log.error(
                    "Telegram не отвечает дольше %d минут — выхожу, Docker перезапустит",
                    RESTART_AFTER // 60,
                )
                # Не рвать запись базы на середине: git, брошенный между rebase и push,
                # ломает все следующие публикации. Ждём, пока лок отпустят.
                for _ in range(24):
                    if not botstate.KB_WRITE_LOCK.locked():
                        break
                    await asyncio.sleep(5)
                os._exit(1)
        await asyncio.sleep(every)


def is_fresh(data_dir: Path, stale_after: int = STALE_AFTER) -> bool:
    """Свежа ли отметка. Используется healthcheck-скриптом внутри контейнера."""
    target = path_for(data_dir)
    try:
        stamp = int(target.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (time.time() - stamp) <= stale_after
