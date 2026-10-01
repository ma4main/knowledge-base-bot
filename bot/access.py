"""Кто есть кто: роль человека и как достучаться до руководителя.

Ничего из обработчиков не импортирует — иначе кольцевой импорт с роутером правок.
Здесь только доступ в личке (по роли человека); в рабочем чате доступ определяется
по чату и живёт в групповом обработчике.
"""

from __future__ import annotations

import logging

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from botstate import ACCESS_ASKED, PENDING_ACCESS
from config import Config
from formatting import to_html
from menu import DENIED
from state import State
from ui import notify_leaders, trim_dict

log = logging.getLogger(__name__)


def sync_leaders(config: Config, app_state: State) -> None:
    """Зеркалит руководителей из состояния бота в живое множество `config.leaders` (одно на процесс)."""
    app_state.import_env_leaders(config.env_leaders)
    config.leaders.clear()
    config.leaders.update(app_state.leader_ids())


def resolve_role(config: Config, app_state: State, user_id: int, username: str | None) -> str | None:
    """Роль с учётом и .env (config), и добавленных из бота менеджеров (state)."""
    role = config.role(user_id, username)
    if role:
        return role
    if app_state.is_extra_manager(user_id, username):
        return "manager"
    return None


def role_of(config: Config, message: Message, app_state: State | None = None) -> str | None:
    user = message.from_user
    if not user:
        return None
    if app_state is not None:
        return resolve_role(config, app_state, user.id, user.username)
    return config.role(user.id, user.username)


async def deny(message: Message, config: Config | None = None) -> None:
    """Отказ в доступе и заявка руководителю с кнопками."""
    user = message.from_user
    if user is None:
        return
    log.info(
        "Отказ в доступе: id=%s username=@%s имя=%s",
        user.id,
        user.username or "нет",
        user.full_name,
    )
    await message.answer(DENIED.format(user_id=user.id), parse_mode="HTML")

    # Заявка — один раз на человека за время работы бота.
    if config is None or user.id in ACCESS_ASKED or not config.leaders:
        return
    ACCESS_ASKED.add(user.id)
    who = f"@{user.username}" if user.username else user.full_name
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Дать доступ", callback_data=f"access:yes:{user.id}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"access:no:{user.id}"),
    ]])
    PENDING_ACCESS[user.id] = (user.full_name, user.username or "")
    trim_dict(PENDING_ACCESS, limit=200)
    await notify_leaders(
        message.bot, config,
        f"🔑 <b>Просит доступ к базе</b>\n"
        f"{to_html(who)} — {to_html(user.full_name)}\n\n"
        f"Это наш менеджер? Дать доступ к базе знаний?",
        keyboard,
    )
