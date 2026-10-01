"""Настройки, меняемые на ходу, — переживают перезапуск контейнера.

Отделено от config.py намеренно: там переменные окружения (задаёт админ сервера),
здесь — то, что руководитель переключает прямо из Telegram.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import atomic


log = logging.getLogger(__name__)

MAX_DIGEST_FACTS = 300


class State:
    def __init__(self, path: Path, default_model: str) -> None:
        self.path = path
        self._data: dict = {"model": default_model}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            self._data.update(json.loads(self.path.read_text(encoding="utf-8")))
            log.info("Состояние загружено: модель %s", self._data.get("model"))
        except Exception:
            log.warning("Файл состояния %s повреждён, беру значения по умолчанию", self.path, exc_info=True)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic.write_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except Exception:
            log.error("Не смог сохранить состояние в %s", self.path, exc_info=True)

    @property
    def model(self) -> str:
        return self._data["model"]

    @model.setter
    def model(self, value: str) -> None:
        self._data["model"] = value
        self._save()
        log.info("Модель переключена на %s", value)

    @property
    def last_published(self) -> datetime | None:
        raw = self._data.get("last_published")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # --- Сводка по чатам ---

    @property
    def last_digest_date(self) -> str:
        return self._data.get("last_digest_date", "")

    @property
    def last_digest_at(self) -> str | None:
        return self._data.get("last_digest_at") or None

    @property
    def last_hypothesis_reminder(self) -> str:
        """Дата последнего напоминания о незакрытых гипотезах."""
        return self._data.get("last_hypothesis_reminder", "")

    def mark_hypothesis_reminder(self) -> None:
        self._data["last_hypothesis_reminder"] = datetime.now().date().isoformat()
        self._save()

    @property
    def last_digest_run(self) -> str | None:
        """Когда последний раз запускался разбор чатов; отдельно от курсора потока `last_digest_at`."""
        return self._data.get("last_digest_run") or None

    def mark_digest_run(self) -> None:
        self._data["last_digest_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._data["last_digest_date"] = datetime.now().date().isoformat()
        self._save()

    def mark_digest(self, at: str | None = None) -> None:
        """Двигает курсор разобранного потока; `at` — до какого момента поток действительно прочитан."""
        now = datetime.now(timezone.utc)
        self._data["last_digest_at"] = at or now.isoformat(timespec="seconds")
        self._save()

    @property
    def last_weekly_at(self) -> str:
        """Дата недельного отчёта «что бот записал сам»."""
        return self._data.get("last_weekly_at", "")

    def mark_weekly(self) -> None:
        self._data["last_weekly_at"] = datetime.now().date().isoformat()
        self._save()

    def auto_edits_since(self, since_date: str) -> list[dict]:
        """Автоправки не раньше указанной даты (YYYY-MM-DD) — для недельного отчёта."""
        return [
            row for row in (self._data.get("auto_journal") or [])
            if (row.get("at") or "")[:10] >= since_date
        ]

    # --- Автономное пополнение базы ---

    @property
    def autonomy(self) -> bool:
        return bool(self._data.get("autonomy", False))

    def set_autonomy(self, on: bool) -> None:
        self._data["autonomy"] = bool(on)
        self._save()
        log.info("Автономное пополнение %s", "включено" if on else "выключено")

    def auto_edits_today(self) -> int:
        today = datetime.now().date().isoformat()
        return int((self._data.get("auto_count") or {}).get(today, 0))

    def note_auto_edit(self, entry: dict) -> None:
        """Записывает автоправку в суточный счётчик и в журнал для утреннего отчёта."""
        today = datetime.now().date().isoformat()
        counts = self._data.setdefault("auto_count", {})
        counts[today] = int(counts.get(today, 0)) + 1
        # Счётчики держим только за последние семь дней.
        for day in [d for d in counts if d < today][:-6]:
            counts.pop(day, None)
        journal = self._data.setdefault("auto_journal", [])
        journal.append({**entry, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        del journal[:-100]
        self._save()

    def auto_journal(self, only_unreported: bool = True) -> list[dict]:
        rows = self._data.get("auto_journal") or []
        return [r for r in rows if not (only_unreported and r.get("reported"))]

    def mark_auto_reported(self) -> None:
        """Помечает записи журнала как показанные в утреннем отчёте."""
        for row in self._data.get("auto_journal") or []:
            row["reported"] = True
        self._save()

    # --- Факты из сводки, ждущие кнопки «Внести» ---

    def remember_digest_fact(self, fact_id: str, text: str, chat: str, verdict: str) -> None:
        facts = self._data.setdefault("digest_facts", {})
        facts[fact_id] = {
            "text": text,
            "chat": chat,
            "verdict": verdict,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Вытесняем сначала разобранное, и только потом самое старое неразобранное.
        if len(facts) > MAX_DIGEST_FACTS:
            order = sorted(
                facts,
                key=lambda k: (not facts[k].get("done"), facts[k].get("at", "")),
            )
            for stale in order[: len(facts) - MAX_DIGEST_FACTS]:
                facts.pop(stale, None)
        self._save()

    def digest_fact(self, fact_id: str) -> dict | None:
        return (self._data.get("digest_facts") or {}).get(fact_id)

    def digest_texts(self, only_open: bool = False) -> dict[str, str]:
        """{id пункта: его текст} — для сверки нового факта с уже показанными."""
        facts = self._data.get("digest_facts") or {}
        return {
            fid: fact.get("text", "")
            for fid, fact in facts.items()
            if not (only_open and fact.get("done"))
        }

    def bump_digest_fact(self, fact_id: str, chat: str) -> dict | None:
        """Отмечает повтор факта: счётчик, откуда и когда повторили."""
        fact = (self._data.get("digest_facts") or {}).get(fact_id)
        if fact is None:
            return None
        fact["repeats"] = int(fact.get("repeats") or 1) + 1
        also = fact.setdefault("also", [])
        if chat and chat not in also:
            also.append(chat)
        fact["last_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save()
        return fact

    def pending_digest_facts(self) -> list[tuple[str, dict]]:
        """Пункты сводок без принятого решения: сначала чаще повторявшиеся, потом свежие."""
        facts = self._data.get("digest_facts") or {}
        rows = [(fid, f) for fid, f in facts.items() if not f.get("done")]
        return sorted(
            rows,
            key=lambda kv: (int(kv[1].get("repeats") or 1), kv[1].get("at", "")),
            reverse=True,
        )

    def expire_digest_facts(self, days: int) -> int:
        """Закрывает пункты, ждущие решения дольше `days` дней. Возвращает, сколько закрыл."""
        facts = self._data.get("digest_facts") or {}
        border = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        closed = 0
        for fact in facts.values():
            # Взятый в работу пункт не трогаем: решение по нему уже принимается.
            if not fact.get("done") and not fact.get("claimed_at") and (fact.get("at") or "") < border:
                fact["done"] = "устарел"
                fact["done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                fact.pop("claimed_at", None)
                closed += 1
        if closed:
            self._save()
        return closed

    def close_digest_fact(self, fact_id: str, how: str) -> bool:
        """Помечает пункт разобранным. `how` — «внесён» или «не надо»."""
        fact = (self._data.get("digest_facts") or {}).get(fact_id)
        if fact is None or fact.get("done"):
            return False
        fact["done"] = how
        fact["done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fact.pop("claimed_at", None)
        self._save()
        return True

    def claim_digest_fact(self, fact_id: str) -> bool:
        """Берёт пункт в работу (не закрывая его). False — уже взят или уже разобран."""
        fact = (self._data.get("digest_facts") or {}).get(fact_id)
        if fact is None or fact.get("done") or fact.get("claimed_at"):
            return False
        fact["claimed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save()
        return True

    def release_digest_fact(self, fact_id: str) -> None:
        """Возвращает пункт в очередь: правка не доехала, решение так и не принято."""
        fact = (self._data.get("digest_facts") or {}).get(fact_id)
        if fact is not None and fact.pop("claimed_at", None) is not None:
            self._save()

    # --- Файлы, предложенные менеджерами и ждущие решения руководителя ---

    def remember_file_proposal(self, fid: str, data: dict) -> None:
        proposals = self._data.setdefault("file_proposals", {})
        proposals[fid] = {**data, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._save()

    def file_proposal(self, fid: str) -> dict | None:
        return (self._data.get("file_proposals") or {}).get(fid)

    def drop_file_proposal(self, fid: str) -> None:
        if (self._data.get("file_proposals") or {}).pop(fid, None) is not None:
            self._save()

    def mark_published(self) -> None:
        self._data["last_published"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save()

    # --- Менеджеры, добавленные из бота (сверх .env). Переживают перезапуск. ---
    # Формат записи: {"id": int|None, "username": str|None, "name": str, "added_by": int}.
    # Роль руководителя — отдельным списком ниже (`leaders`), только по числовому id.

    @property
    def extra_managers(self) -> list[dict]:
        return self._data.get("extra_managers", [])

    def is_extra_manager(self, user_id: int, username: str | None) -> bool:
        nick = (username or "").lstrip("@").lower()
        for m in self.extra_managers:
            if m.get("id") and m["id"] == user_id:
                return True
            if nick and m.get("username") and m["username"].lstrip("@").lower() == nick:
                return True
        return False

    def add_manager(
        self, added_by: int, user_id: int | None = None, username: str | None = None, name: str = ""
    ) -> bool:
        """Добавляет менеджера. Возвращает False, если уже есть."""
        if self.is_extra_manager(user_id or 0, username):
            return False
        entry = {
            "id": user_id,
            "username": (username or "").lstrip("@").lower() or None,
            "name": name,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._data.setdefault("extra_managers", []).append(entry)
        self._save()
        log.info("Добавлен менеджер: %s (добавил %s)", entry, added_by)
        return True

    def note_manager_id(self, user_id: int, username: str | None, name: str = "") -> None:
        """Дописывает числовой id менеджеру, добавленному по нику."""
        nick = (username or "").lstrip("@").lower()
        if not nick:
            return
        for row in self.extra_managers:
            if not row.get("id") and (row.get("username") or "") == nick:
                row["id"] = int(user_id)
                if name and not row.get("name"):
                    row["name"] = name
                self._save()
                log.info("Менеджеру @%s закреплён id %s", nick, user_id)
                return

    # --- Критичные сигналы руководителям (alerts.py): когда о чём писали. ---

    def alert_sent_on(self, kind: str) -> str:
        return (self._data.get("alerts") or {}).get(kind, "")

    def mark_alert(self, kind: str, day: str) -> None:
        self._data.setdefault("alerts", {})[kind] = day
        self._save()

    # --- Кто писал боту: id → имя и ник. ---

    @property
    def users(self) -> dict[str, dict]:
        return self._data.get("users", {})

    def note_user(self, user_id: int, username: str | None, name: str) -> None:
        """Запоминает человека с доступом. Пишет на диск только при изменении."""
        row = {"name": name or "", "username": (username or "").lstrip("@").lower()}
        if self.users.get(str(user_id)) == row:
            return
        self._data.setdefault("users", {})[str(user_id)] = row
        self._save()

    def user_label(self, user_id: int) -> str:
        """«Имя (@ник)» для списков; числовой id не показываем."""
        row = self.users.get(str(user_id)) or {}
        name, nick = row.get("name") or "", row.get("username") or ""
        if not name:
            for m in self.extra_managers:
                if m.get("id") == user_id:
                    name, nick = m.get("name") or "", nick or (m.get("username") or "")
                    break
        if not name:
            for m in self.leaders:
                if int(m.get("id") or 0) == user_id and m.get("name"):
                    name = m["name"]
                    break
        if name and nick:
            return f"{name} (@{nick})"
        return name or (f"@{nick}" if nick else "")

    # --- Руководители. Источник правды — здесь, а не в .env. ---
    # Формат записи: {"id": int, "name": str, "added_by": int, "added_at": iso}.
    # Только числовой id: ник переназначаем (см. Config.role).

    @property
    def leaders(self) -> list[dict]:
        return self._data.get("leaders", [])

    def leader_ids(self) -> set[int]:
        return {int(row["id"]) for row in self.leaders if row.get("id")}

    def import_env_leaders(self, env_ids) -> list[int]:
        """Переносит руководителей из .env в состояние, каждый id ровно один раз. Возвращает, кого добавил."""
        seen = set(self._data.get("leaders_env_seen", []))
        added: list[int] = []
        for uid in sorted(set(env_ids) - seen):
            seen.add(uid)
            if uid not in self.leader_ids():
                self._data.setdefault("leaders", []).append({
                    "id": uid, "name": "", "added_by": 0,
                    "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
                added.append(uid)
        if set(self._data.get("leaders_env_seen", [])) != seen:
            self._data["leaders_env_seen"] = sorted(seen)
            self._save()
        if added:
            log.info("Руководители из .env перенесены в состояние: %s", added)
        return added

    def add_leader(self, added_by: int, user_id: int, name: str = "") -> bool:
        """Назначает руководителя. False — если уже руководитель."""
        if user_id in self.leader_ids():
            return False
        self._data.setdefault("leaders", []).append({
            "id": int(user_id), "name": name, "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self._save()
        log.info("Назначен руководитель %s (%s), назначил %s", user_id, name, added_by)
        return True

    def remove_leader(self, user_id: int) -> bool:
        """Снимает роль руководителя. False — если такого нет или он последний."""
        rows = self.leaders
        if user_id not in self.leader_ids() or len(rows) <= 1:
            return False
        self._data["leaders"] = [r for r in rows if int(r.get("id") or 0) != user_id]
        self._save()
        log.info("Снят руководитель %s", user_id)
        return True

    def name_leader(self, user_id: int, name: str) -> None:
        """Запоминает имя руководителя, когда он впервые пишет боту."""
        for row in self.leaders:
            if int(row.get("id") or 0) == user_id and name and row.get("name") != name:
                row["name"] = name
                self._save()
                return

    # --- Рабочие чаты, которые бот слушает. ---

    @property
    def chats(self) -> dict[str, dict]:
        return self._data.setdefault("chats", {})

    def is_listening(self, chat_id: int, thread_id: int = 0) -> bool:
        """Слушаем, пока чат или конкретный топик в нём не заглушили явно; незнакомый чат — слушаем."""
        entry = self.chats.get(str(chat_id), {})
        if entry.get("muted", False):
            return False
        if thread_id and str(thread_id) in (entry.get("muted_topics") or {}):
            return False
        return True

    def muted_topics(self, chat_id: int) -> dict[str, str]:
        """{thread_id: название} заглушённых топиков чата."""
        return dict(self.chats.get(str(chat_id), {}).get("muted_topics") or {})

    def mute_topic(self, chat_id: int, thread_id: int, name: str = "") -> bool:
        """Перестать записывать один топик. False — если он и так заглушён."""
        if not thread_id:
            return False
        entry = self.chats.setdefault(str(chat_id), {"title": "", "muted": False})
        muted = entry.setdefault("muted_topics", {})
        key = str(thread_id)
        if key in muted:
            return False
        # Имя топика Telegram отдаёт только при его создании; неизвестное заменяем номером.
        muted[key] = name or self.topic_name(chat_id, thread_id) or f"топик {thread_id}"
        self._save()
        log.info("Чат %s: топик %s («%s») заглушён", chat_id, thread_id, muted[key])
        return True

    def unmute_topic(self, chat_id: int, thread_id: int) -> bool:
        entry = self.chats.get(str(chat_id))
        if not thread_id or entry is None:
            return False
        muted = entry.get("muted_topics") or {}
        if str(thread_id) not in muted:
            return False
        muted.pop(str(thread_id))
        self._save()
        log.info("Чат %s: топик %s снова записывается", chat_id, thread_id)
        return True

    # --- Наблюдённые подписи людей: «@ник → имя из профиля Telegram» ---

    @property
    def people(self) -> dict[str, str]:
        return self._data.setdefault("people", {})

    def note_person(self, username: str | None, full_name: str) -> bool:
        """Запоминает «ник → имя». True — если запись новая или имя изменилось; без ника не пишем."""
        nick = (username or "").lstrip("@").lower()
        name = (full_name or "").strip()
        if not nick or not name:
            return False
        if self.people.get(nick) == name:
            return False
        self.people[nick] = name
        self._save()
        log.info("Запомнил подпись: @%s — %s", nick, name)
        return True

    def note_chat(self, chat_id: int, title: str) -> bool:
        """Запоминает чат и обновляет название. True — если чат встретился впервые."""
        key = str(chat_id)
        known = self.chats.get(key)
        if known is None:
            self.chats[key] = {
                "title": title,
                "muted": False,
                "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._save()
            log.info("Новый чат в записи: %s («%s»)", chat_id, title)
            return True
        if title and known.get("title") != title:
            known["title"] = title
            self._save()
        return False

    def mute_chat(self, chat_id: int, title: str = "") -> bool:
        """Перестать записывать чат. False — если он и так заглушён."""
        key = str(chat_id)
        entry = self.chats.setdefault(
            key, {"title": title, "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        )
        if entry.get("muted"):
            return False
        entry["muted"] = True
        self._save()
        log.info("Чат %s («%s») заглушён — запись прекращена", chat_id, entry.get("title", ""))
        return True

    def note_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        """Запоминает название топика: Telegram присылает его только при создании топика."""
        if not thread_id or not name:
            return
        entry = self.chats.setdefault(str(chat_id), {"title": "", "muted": False})
        topics = entry.setdefault("topics", {})
        if topics.get(str(thread_id)) != name:
            topics[str(thread_id)] = name
            self._save()
            log.info("Чат %s: топик %s = «%s»", chat_id, thread_id, name)

    def topic_name(self, chat_id: int, thread_id: int) -> str:
        if not thread_id:
            return ""
        return self.chats.get(str(chat_id), {}).get("topics", {}).get(str(thread_id), "")

    def unmute_chat(self, chat_id: int) -> bool:
        entry = self.chats.get(str(chat_id))
        if entry is None or not entry.get("muted"):
            return False
        entry["muted"] = False
        self._save()
        log.info("Чат %s снова записывается", chat_id)
        return True

    def remove_manager(self, key: str) -> bool:
        """Удаляет менеджера по id (число) или нику. Возвращает True, если удалил."""
        key = key.strip().lstrip("@").lower()
        before = self.extra_managers
        kept = []
        removed = False
        for m in before:
            if str(m.get("id")) == key or (m.get("username") or "") == key:
                removed = True
                continue
            kept.append(m)
        if removed:
            self._data["extra_managers"] = kept
            self._save()
        return removed
