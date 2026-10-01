"""Конфигурация бота. Всё читается из переменных окружения — секретов в репозитории нет."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _ids(raw: str) -> set[int]:
    """Разбирает список Telegram user_id: "123, 456" → {123, 456}."""
    return {int(x) for x in raw.replace(",", " ").split() if x.strip()}


def _usernames(raw: str) -> set[str]:
    """Разбирает список ников: "@Vasya, petya" → {"vasya", "petya"}.

    Ник приводим к нижнему регистру и без @: Telegram их так и сравнивает.
    """
    return {x.lstrip("@").lower() for x in raw.replace(",", " ").split() if x.strip()}


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Проверь .env (образец — bot/.env.example)."
        )
    return value


# Запасные модели, если в .env их не задали. Отключить: OPENROUTER_MODEL_FALLBACKS=none.
# Первая — того же семейства, что основная, вторая — другого провайдера.
DEFAULT_FALLBACKS = ("google/gemini-3.6-flash", "openai/gpt-5.6-luna")


@dataclass(frozen=True)
class Config:
    telegram_token: str
    openrouter_key: str

    # Путь к клону базы знаний. По умолчанию — корень репозитория (bot/ лежит внутри него).
    kb_root: Path

    # Папка под то, что бот пишет сам: выбранная модель, журнал расхода.
    # Отдельно от базы знаний — та примонтирована только на чтение.
    data_dir: Path

    # Роли: менеджер и руководитель (руководитель имеет и права менеджера).
    # Менеджер — по числовому id или по нику; руководитель — только по id: ник
    # переназначаем, и освободившийся ник мог бы перехватить посторонний.
    #
    # `leaders` — живое множество: Config заморожен, но само множество изменяемо.
    # Источник правды — `State.leaders`, сюда оно зеркалится через
    # `access.sync_leaders`. `LEADER_IDS` из .env — начальная загрузка и аварийный вход.
    managers: set[int]
    leaders: set[int]
    manager_usernames: set[str]
    # Что было прописано в .env — чтобы State знал, кого импортировать.
    env_leaders: frozenset[int]

    model: str
    model_fallbacks: list[str] = field(default_factory=list)

    # Дешёвая модель для классификатора типа запроса (qtype.py); не ответила — основная.
    classifier_model: str = "google/gemini-3.5-flash-lite"

    # Сверять ли фразу для клиента с текстом единиц (factcheck.py). CLIENT_PHRASE_AUDIT=0 выключает.
    audit_client_phrases: bool = True

    # Кэш префикса: "1h" заметно дешевле при десятках вопросов в день, "5m" — при редких.
    cache_ttl: str = "1h"

    max_tool_iterations: int = 6
    request_timeout: int = 120
    log_level: str = "INFO"

    @property
    def has_whitelist(self) -> bool:
        return bool(
            self.managers
            | self.leaders
            | self.manager_usernames
        )

    def role(self, user_id: int, username: str | None = None) -> str | None:
        nick = (username or "").lstrip("@").lower()
        # Руководитель — только по числовому id, ник для этой роли не проверяется.
        if user_id in self.leaders:
            return "leader"
        if user_id in self.managers or nick in self.manager_usernames:
            return "manager"
        return None

    @classmethod
    def from_env(cls) -> "Config":
        kb_root = Path(
            os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent
        ).resolve()
        if not (kb_root / "knowledge" / "INDEX.md").is_file():
            raise RuntimeError(
                f"В {kb_root} не найден knowledge/INDEX.md — KB_ROOT указывает не на базу знаний."
            )

        fallbacks = [
            m.strip()
            for m in os.environ.get("OPENROUTER_MODEL_FALLBACKS", "").split(",")
            if m.strip() and m.strip().lower() != "none"
        ]
        # Без явного значения берём запасные по умолчанию: сняли модель с обслуживания —
        # бот не должен встать. OpenRouter сам переходит к следующей.
        if not fallbacks and os.environ.get("OPENROUTER_MODEL_FALLBACKS", "").strip().lower() != "none":
            fallbacks = list(DEFAULT_FALLBACKS)

        data_dir = Path(
            os.environ.get("DATA_DIR") or Path(__file__).resolve().parent / "data"
        )
        data_dir.mkdir(parents=True, exist_ok=True)

        # SUPER_ADMIN_IDS — прежнее имя переменной, читается для совместимости.
        env_leaders = _ids(os.environ.get("LEADER_IDS", "")) | _ids(
            os.environ.get("SUPER_ADMIN_IDS", "")
        )

        return cls(
            telegram_token=_require("TELEGRAM_BOT_TOKEN"),
            openrouter_key=_require("OPENROUTER_API_KEY"),
            kb_root=kb_root,
            data_dir=data_dir,
            managers=_ids(os.environ.get("MANAGER_IDS", "")),
            # set() — отдельное изменяемое множество: его наполняет access.sync_leaders.
            leaders=set(env_leaders),
            # Ник из прежнего SUPER_ADMIN_USERNAMES даёт только права менеджера.
            manager_usernames=_usernames(os.environ.get("MANAGER_USERNAMES", ""))
            | _usernames(os.environ.get("SUPER_ADMIN_USERNAMES", "")),
            env_leaders=frozenset(env_leaders),
            model=os.environ.get("OPENROUTER_MODEL", "google/gemini-3.7-flash"),
            model_fallbacks=fallbacks,
            classifier_model=os.environ.get(
                "CLASSIFIER_MODEL", "google/gemini-3.5-flash-lite"
            ),
            audit_client_phrases=os.environ.get("CLIENT_PHRASE_AUDIT", "1").strip()
            not in {"0", "false", "no", "нет"},
            cache_ttl=os.environ.get("CACHE_TTL", "1h"),
            max_tool_iterations=int(os.environ.get("MAX_TOOL_ITERATIONS", "6")),
            request_timeout=int(os.environ.get("REQUEST_TIMEOUT", "120")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )
