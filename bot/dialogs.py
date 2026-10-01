"""Разговоры менеджеров с ботом, переживающие перезапуск.

Разговор живёт в пределах дня (TTL_HOURS) и хранит только последние сообщения
(HISTORY_LIMIT в agent.py). Формат — один JSON-файл в данных бота, в git не попадает.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import atomic

from agent import Dialog

log = logging.getLogger(__name__)

# Сколько разговор живёт без новых сообщений.
TTL_HOURS = 12


class DialogStore:
    def __init__(self, path: Path, ttl_hours: int = TTL_HOURS) -> None:
        self.path = path
        self.ttl = timedelta(hours=ttl_hours)
        self._dialogs: dict[int, Dialog] = {}
        self._seen: dict[int, datetime] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Файл разговоров повреждён — начинаю с пустых", exc_info=True)
            return
        now = datetime.now(timezone.utc)
        for key, row in (raw or {}).items():
            try:
                seen = datetime.fromisoformat(row["at"])
                if now - seen > self.ttl:
                    continue
                dialog = Dialog()
                dialog.messages = list(row.get("messages") or [])
                dialog.kind = str(row.get("kind") or "")
                self._dialogs[int(key)] = dialog
                self._seen[int(key)] = seen
            except (KeyError, ValueError, TypeError):
                continue
        log.info("Разговоров загружено: %d", len(self._dialogs))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                str(user_id): {
                    "at": self._seen[user_id].isoformat(timespec="seconds"),
                    "messages": dialog.messages,
                    "kind": dialog.kind,
                }
                for user_id, dialog in self._dialogs.items()
            }
            atomic.write_text(self.path, json.dumps(data, ensure_ascii=False))
        except Exception:
            log.error("Не смог сохранить разговоры в %s", self.path, exc_info=True)

    def get(self, user_id: int) -> Dialog:
        """Разговор человека. Протухший (молчал дольше срока) начинается заново."""
        seen = self._seen.get(user_id)
        if seen is not None and datetime.now(timezone.utc) - seen > self.ttl:
            log.info("Разговор с %s протух (молчание дольше %s) — начинаю заново", user_id, self.ttl)
            self._dialogs.pop(user_id, None)
            self._seen.pop(user_id, None)
        return self._dialogs.setdefault(user_id, Dialog())

    def touch(self, user_id: int) -> None:
        """Обновляет отметку времени разговора и сохраняет на диск."""
        self._seen[user_id] = datetime.now(timezone.utc)
        self._prune()
        self._save()

    def reset(self, user_id: int) -> None:
        """Забывает разговор сразу, в том числе на диске."""
        self._dialogs.pop(user_id, None)
        self._seen.pop(user_id, None)
        self._save()

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        stale = [uid for uid, seen in self._seen.items() if now - seen > self.ttl]
        for user_id in stale:
            self._dialogs.pop(user_id, None)
            self._seen.pop(user_id, None)
