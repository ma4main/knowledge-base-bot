"""Меню кнопками и управление людьми: экраны по дереву `menu.TREE`, выдача
и снятие доступа менеджера, назначение и снятие руководителя.
"""

from __future__ import annotations

import inspect
import logging
from typing import Awaitable, Callable

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from access import deny, resolve_role, sync_leaders
from botstate import (
    PENDING_ACCESS,
    PENDING_MENU_INPUT,
    PENDING_SUGGESTION,
    PENDING_UPDATE,
    forget_pending_text,
    is_cancel,
)
from config import Config
from formatting import to_html
from menu import (
    BACK,
    BTN_MENU,
    GREETING,
    HOWTO_ADD,
    LEADER,
    LEADER_HELP,
    MAIN_KEYBOARD,
    MANAGER_HELP,
    MENU_LABELS,
    TREE,
    allowed,
    find_item,
    screen_markup,
)
from state import State
from ui import notify_manager

log = logging.getLogger(__name__)
router = Router()

# Обработчики команд из других модулей: имя → обработчик. `handlers` регистрирует
# их сюда через register(), обратный импорт замкнул бы кольцо.
LEAVES: dict[str, Callable[..., Awaitable]] = {}


def register(**leaves: Callable[..., Awaitable]) -> None:
    LEAVES.update(leaves)


def _as_user_message(callback: CallbackQuery, text: str = "") -> Message:
    """Сообщение бота под кнопкой → «сообщение от нажавшего», чтобы проверка роли
    в обработчике команды сработала как обычно."""
    return callback.message.model_copy(update={"from_user": callback.from_user, "text": text})


async def _call(leaf: Callable[..., Awaitable], message: Message, data: dict) -> None:
    """Зовёт обработчик команды, подобрав зависимости по именам параметров, как aiogram."""
    wanted = inspect.signature(leaf).parameters
    kwargs = {name: data[name] for name in wanted if name in data}
    await leaf(message, **kwargs)


async def send_menu(message: Message, role: str) -> None:
    await message.answer(
        TREE["root"].title, parse_mode="HTML", reply_markup=screen_markup("root", role)
    )


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == BTN_MENU, F.chat.type == ChatType.PRIVATE)
async def on_menu(message: Message, config: Config, app_state: State) -> None:
    user = message.from_user
    role = resolve_role(config, app_state, user.id, user.username) if user else None
    if not role:
        await deny(message, config)
        return
    app_state.note_user(user.id, user.username, user.full_name)
    forget_pending_text(user.id)
    await send_menu(message, role)


# --- Ввод по запросу кнопки ---------------------------------------------------


def _awaiting_input(message: Message) -> bool:
    """Фильтр с намеренным побочным эффектом: команда или подпись кнопки вместо
    ожидаемого текста снимает режим ввода и отдаёт сообщение его обработчику."""
    user = message.from_user
    if user is None or user.id not in PENDING_MENU_INPUT:
        return False
    text = (message.text or "").strip()
    if text.startswith("/") or text in MENU_LABELS:
        PENDING_MENU_INPUT.pop(user.id, None)
        return False
    return True


@router.message(F.chat.type == ChatType.PRIVATE, F.text, _awaiting_input)
async def on_menu_input(message: Message, **data) -> None:
    config: Config = data["config"]
    app_state: State = data["app_state"]
    user = message.from_user
    kind = PENDING_MENU_INPUT.pop(user.id, "")
    text = (message.text or "").strip()
    role = resolve_role(config, app_state, user.id, user.username)
    if role != LEADER:
        await message.answer("Это действие доступно руководителю.")
        return
    if is_cancel(text):
        await message.answer("Ок, отменил.", reply_markup=MAIN_KEYBOARD)
        return

    if kind == "compare":
        leaf = LEAVES.get("compare")
        if leaf is not None:
            await _call(leaf, message.model_copy(update={"text": f"/compare {text}"}), data)
        return

    if kind == "add_manager":
        key = (text.split() or [""])[0].lstrip("@")
        if not key:
            await message.answer("Не понял. Пришли ник, например @ivanov.")
            return
        if key.isdigit():
            ok = app_state.add_manager(user.id, user_id=int(key))
            who = key
        else:
            ok = app_state.add_manager(user.id, username=key)
            who = "@" + key
        await message.answer(
            f"✅ {who} теперь менеджер: может спрашивать базу и вносить в неё."
            if ok else f"{who} уже в списке."
        )
        if ok and key.isdigit():
            await notify_manager(
                message.bot, int(key),
                "Доступ к базе знаний открыт. Спрашивай своими словами; всё остальное — "
                "по кнопке «☰ Меню». Начни с /start.",
            )
        await _send_people(message, config, app_state)
        return


# --- Нажатия меню --------------------------------------------------------------


@router.callback_query(F.data.startswith("m:"))
async def on_menu_button(callback: CallbackQuery, **data) -> None:
    config: Config = data["config"]
    app_state: State = data["app_state"]
    user = callback.from_user
    role = resolve_role(config, app_state, user.id, user.username)
    if not role:
        await callback.answer("Нет доступа", show_alert=True)
        return
    app_state.note_user(user.id, user.username, user.full_name)
    if not isinstance(callback.message, Message):
        # Для старых сообщений Telegram отдаёт только заглушку — править нечего.
        await callback.answer("Это меню устарело — открой новое: /menu", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    kind = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    rest = ":".join(parts[3:])

    if kind == "nav":
        screen = TREE.get(arg)
        if screen is None or not allowed(screen, role):
            await callback.answer("Этот раздел доступен руководителю", show_alert=True)
            return
        await callback.answer()
        await _edit(callback, screen.title, screen_markup(arg, role))
        return

    if kind == "ppl":
        if role != LEADER:
            await callback.answer("Доступно руководителю", show_alert=True)
            return
        await _people_action(callback, config, app_state, arg, rest)
        return

    if kind == "auto":
        if role != LEADER:
            await callback.answer("Доступно руководителю", show_alert=True)
            return
        app_state.set_autonomy(arg == "on")
        await callback.answer("Включил" if arg == "on" else "Выключил")
        text, markup = _autonomy_view(app_state)
        await _edit(callback, text, markup)
        return

    if kind != "do":
        await callback.answer()
        return

    item = find_item(f"do:{arg}")
    if item is None or not allowed(item, role):
        await callback.answer("Доступно руководителю", show_alert=True)
        return
    await callback.answer()
    await _do(callback, arg, role, data)


async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        # «message is not modified» — ту же кнопку нажали дважды.
        log.debug("Меню не изменилось", exc_info=True)


async def _do(callback: CallbackQuery, action: str, role: str, data: dict) -> None:
    config: Config = data["config"]
    app_state: State = data["app_state"]
    user = callback.from_user
    chat = callback.message

    if action == "update":
        forget_pending_text(user.id)
        PENDING_UPDATE.add(user.id)
        await chat.answer(
            "Напиши одним сообщением, что внести или поправить, — своими словами, "
            "например «минимальный срок договора теперь три месяца».\n\n"
            "Найду подходящую тему, покажу, как именно запишу, и спрошу «верно?». "
            "В базу попадёт только после твоего «да» — с твоим именем и датой. "
            "Передумал — напиши «отмена»."
        )
        return
    if action == "idea":
        forget_pending_text(user.id)
        PENDING_SUGGESTION.add(user.id)
        await chat.answer(
            "Напиши идею или пожелание одним сообщением — передам руководителю.\n"
            "Это про работу бота и процессы. Дополнить саму базу знаний — "
            "кнопка «✏️ Внести в базу». Передумал — напиши «отмена»."
        )
        return
    if action == "help":
        await chat.answer(
            GREETING + (LEADER_HELP if role == LEADER else MANAGER_HELP), parse_mode="HTML"
        )
        return
    if action == "howto_add":
        await chat.answer(HOWTO_ADD, parse_mode="HTML")
        return
    if action == "whoami":
        title = {"leader": "руководитель", "manager": "менеджер"}.get(role, role)
        await chat.answer(
            f"Твой Telegram ID: <code>{user.id}</code>\n"
            f"Ник: @{to_html(user.username or 'нет')}\n"
            f"Роль: {title}",
            parse_mode="HTML",
        )
        return
    if action == "reset":
        dialog_store = data.get("dialog_store")
        if dialog_store is not None:
            dialog_store.reset(user.id)
        forget_pending_text(user.id)
        await chat.answer("Разговор сброшен. Спрашивай заново.")
        return
    if action == "people":
        text, markup = _people_view(config, app_state)
        await _edit(callback, text, markup)
        return
    if action == "autonomy":
        text, markup = _autonomy_view(app_state)
        await _edit(callback, text, markup)
        return
    if action == "compare":
        forget_pending_text(user.id)
        PENDING_MENU_INPUT[user.id] = "compare"
        await chat.answer(
            "Напиши вопрос — задам его нескольким моделям сразу и покажу ответы рядом. "
            "Передумал — «отмена»."
        )
        return

    leaf = LEAVES.get(action)
    if leaf is None:
        log.error("Пункт меню «%s» ни к чему не привязан", action)
        await chat.answer("Этот пункт пока не работает — скажи руководителю.")
        return
    await _call(leaf, _as_user_message(callback, f"/{action}"), data)


# --- Автозапись ----------------------------------------------------------------


def _autonomy_view(app_state: State) -> tuple[str, InlineKeyboardMarkup]:
    on = app_state.autonomy
    text = (
        f"<b>Автозапись из чатов: {'включена' if on else 'выключена'}</b>\n\n"
        "Каждый вечер я разбираю рабочие чаты и сам записываю в базу новое: факты, "
        "уточнения правил, новые темы. Всё записанное так помечается «не подтверждено» "
        "и попадает в недельный отчёт с кнопкой «Откатить». Существующий текст "
        "не удаляю и не переписываю никогда.\n\n"
        f"Сегодня записал сам: {app_state.auto_edits_today()}.\n\n"
        "<i>На запись по просьбе человека («запомни …» с подтверждением "
        "формулировки) этот выключатель не влияет.</i>"
    )
    rows = [
        [InlineKeyboardButton(
            text="⏸ Выключить" if on else "▶️ Включить",
            callback_data=f"m:auto:{'off' if on else 'on'}",
        )],
        [InlineKeyboardButton(text=BACK, callback_data="m:nav:botcfg")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# --- Люди и доступ -------------------------------------------------------------
#
# Человека показываем по имени и нику, а не по числовому id; сам id остаётся
# основой роли и запоминается, когда человек пишет боту (`State.note_user`).

UNKNOWN_NAME = "имя узнаю, когда напишет боту"


def _who(app_state: State, user_id: int) -> str:
    return app_state.user_label(user_id) or UNKNOWN_NAME


def _leader_name(row: dict, app_state: State) -> str:
    return _who(app_state, int(row.get("id") or 0))


def _manager_label(m: dict) -> str:
    nick = f"@{m['username']}" if m.get("username") else ""
    name = m.get("name") or ""
    if name and nick:
        return f"{name} ({nick})"
    return name or nick or UNKNOWN_NAME


def _manager_key(m: dict) -> str:
    """Ключ менеджера для callback_data и `State.remove_manager`: id надёжнее ника."""
    return str(m["id"]) if m.get("id") else str(m.get("username") or "")


def _leader_candidates(config: Config, app_state: State) -> tuple[list[int], list[str]]:
    """Кого можно назначить руководителем. Возвращает (id тех, кого можно выбрать;
    подписи тех, кто получил доступ по нику и боту ещё не писал — их id неизвестен)."""
    leaders = app_state.leader_ids()
    ids: set[int] = set(config.managers)
    ids |= {int(m["id"]) for m in app_state.extra_managers if m.get("id")}
    for raw, row in app_state.users.items():
        if raw.isdigit() and resolve_role(config, app_state, int(raw), row.get("username")):
            ids.add(int(raw))
    waiting = [
        f"@{m['username']}" for m in app_state.extra_managers
        if not m.get("id") and m.get("username")
    ]
    ordered = sorted(ids - leaders, key=lambda uid: (_who(app_state, uid) == UNKNOWN_NAME,
                                                     _who(app_state, uid).lower()))
    return ordered, waiting


def _people_view(config: Config, app_state: State) -> tuple[str, InlineKeyboardMarkup]:
    leaders = app_state.leaders
    extra = app_state.extra_managers
    lines = ["<b>👥 Люди и доступ</b>", "", f"<b>Руководители — {len(leaders)}</b>"]
    for row in leaders:
        lines.append(f"• {to_html(_leader_name(row, app_state))}")
    extra_ids = {m.get("id") for m in extra}
    env_people = [
        _who(app_state, uid) for uid in sorted(config.managers)
        if uid not in extra_ids and uid not in app_state.leader_ids()
    ] + [f"@{nick}" for nick in sorted(config.manager_usernames)]
    lines += ["", f"<b>Менеджеры — {len(extra) + len(env_people)}</b>"]
    lines += [f"• {to_html(_manager_label(m))}" for m in extra]
    lines += [f"• {to_html(name)} <i>(задан на сервере)</i>" for name in env_people]
    if not extra and not env_people:
        lines.append("—")
    lines += [
        "",
        "<i>Руководитель выдаёт и забирает доступ, назначает и снимает руководителей. "
        "Снятый руководитель остаётся менеджером.</i>",
    ]
    rows = [
        [
            InlineKeyboardButton(text="➕ Менеджер", callback_data="m:ppl:addm"),
            InlineKeyboardButton(text="➖ Менеджер", callback_data="m:ppl:rmm"),
        ],
        [
            InlineKeyboardButton(text="➕ Руководитель", callback_data="m:ppl:addl"),
            InlineKeyboardButton(text="➖ Руководитель", callback_data="m:ppl:rml"),
        ],
    ]
    if PENDING_ACCESS:
        rows.append([InlineKeyboardButton(
            text=f"🔑 Заявки на доступ — {len(PENDING_ACCESS)}", callback_data="m:ppl:req",
        )])
    rows.append([InlineKeyboardButton(text=BACK, callback_data="m:nav:root")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_people(message: Message, config: Config, app_state: State) -> None:
    text, markup = _people_view(config, app_state)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


def _back_to_people() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=BACK, callback_data="m:do:people")]


async def _confirm_leader(target: Message, app_state: State, user_id: int) -> None:
    """Роль даёт управление доступами, поэтому назначаем только после явного «да»."""
    if user_id in app_state.leader_ids():
        await target.answer("Этот человек уже руководитель.")
        return
    who = _who(app_state, user_id)
    await target.answer(
        f"Назначить руководителем: <b>{to_html(who)}</b>?\n\n"
        "Руководитель выдаёт и забирает доступы, назначает и снимает других "
        "руководителей, получает недельный отчёт и заявки на доступ.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Назначить", callback_data=f"m:ppl:addl2:{user_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="m:do:people"),
        ]]),
    )


async def _people_action(
    callback: CallbackQuery, config: Config, app_state: State, action: str, arg: str
) -> None:
    actor = callback.from_user
    chat = callback.message

    if action == "addm":
        await callback.answer()
        forget_pending_text(actor.id)
        PENDING_MENU_INPUT[actor.id] = "add_manager"
        await chat.answer(
            "Проще всего: пусть человек сам напишет мне /start — тебе придёт заявка "
            "с кнопкой «Дать доступ».\n\n"
            "Либо пришли сюда его ник, например @ivanov. Передумал — «отмена»."
        )
        return

    if action == "rmm":
        await callback.answer()
        extra = app_state.extra_managers
        if not extra:
            await _edit(callback, "Менеджеров, добавленных из бота, нет.",
                        InlineKeyboardMarkup(inline_keyboard=[_back_to_people()]))
            return
        rows = [
            [InlineKeyboardButton(
                text=_manager_label(m)[:48], callback_data=f"m:ppl:rmm1:{_manager_key(m)}",
            )]
            for m in extra[:40]
        ]
        rows.append(_back_to_people())
        await _edit(callback, "<b>У кого забрать доступ?</b>",
                    InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if action == "rmm1":
        await callback.answer()
        await _edit(
            callback,
            f"Забрать доступ у <b>{to_html(_label_by_key(app_state, arg))}</b>? "
            "Он больше не сможет спрашивать базу.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Забрать", callback_data=f"m:ppl:rmm2:{arg}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="m:do:people"),
            ]]),
        )
        return

    if action == "rmm2":
        if arg.isdigit() and int(arg) in app_state.leader_ids():
            await callback.answer(
                "Это руководитель. Сначала сними роль руководителя.", show_alert=True
            )
            return
        ok = app_state.remove_manager(arg)
        await callback.answer("Доступ забрал" if ok else "Уже не в списке")
        log.info("Доступ менеджера %s забрал %s: %s", arg, actor.id, ok)
        text, markup = _people_view(config, app_state)
        await _edit(callback, text, markup)
        return

    if action == "addl":
        await callback.answer()
        candidates, waiting = _leader_candidates(config, app_state)
        rows = [
            [InlineKeyboardButton(
                text=_who(app_state, uid)[:56], callback_data=f"m:ppl:addl1:{uid}",
            )]
            for uid in candidates[:40]
        ]
        rows.append(_back_to_people())
        text = "<b>Кого назначить руководителем?</b>\nВыбери из тех, у кого уже есть доступ."
        if not candidates:
            text += "\n\nПока выбирать не из кого: сначала дай человеку доступ («➕ Менеджер»)."
        if waiting:
            text += (
                "\n\n<i>Доступ выдан, но боту ещё не писали: "
                + to_html(", ".join(waiting))
                + ". Попроси написать мне любое сообщение — появятся в списке.</i>"
            )
        await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if action == "addl1":
        await callback.answer()
        if arg.isdigit():
            await _confirm_leader(chat, app_state, int(arg))
        return

    if action == "addl2":
        if not arg.isdigit():
            await callback.answer()
            return
        new_id = int(arg)
        name = (app_state.users.get(str(new_id)) or {}).get("name") or next(
            (m.get("name") or "" for m in app_state.extra_managers if m.get("id") == new_id), ""
        )
        ok = app_state.add_leader(actor.id, new_id, name)
        sync_leaders(config, app_state)
        await callback.answer("Назначил" if ok else "Уже руководитель")
        if ok:
            log.info("Руководитель %s назначен пользователем %s", new_id, actor.id)
            await notify_manager(
                callback.bot, new_id,
                "Тебя назначили руководителем базы знаний. Теперь тебе приходят заявки "
                "на доступ и недельный отчёт, а в «☰ Меню» появились «Люди и доступ» "
                "и «Управление». Открой меню: /menu",
            )
            await _tell_other_leaders(
                callback, config,
                f"👥 Назначен новый руководитель: {_who(app_state, new_id)}. "
                f"Назначил {actor.full_name}.",
            )
        text, markup = _people_view(config, app_state)
        await _edit(callback, text, markup)
        return

    if action == "rml":
        await callback.answer()
        leaders = app_state.leaders
        if len(leaders) <= 1:
            await _edit(
                callback,
                "Руководитель один — снять его нельзя: некому будет выдавать доступы.\n"
                "Сначала назначь второго («➕ Руководитель»), потом снимай.",
                InlineKeyboardMarkup(inline_keyboard=[_back_to_people()]),
            )
            return
        rows = [
            [InlineKeyboardButton(
                text=_leader_name(row, app_state)[:56],
                callback_data=f"m:ppl:rml1:{row.get('id')}",
            )]
            for row in leaders
        ]
        rows.append(_back_to_people())
        await _edit(callback, "<b>С кого снять роль руководителя?</b>",
                    InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if action == "rml1":
        await callback.answer()
        mine = arg.isdigit() and int(arg) == actor.id
        await _edit(
            callback,
            ("Снять роль руководителя <b>с себя</b>?" if mine
             else f"Снять роль руководителя: <b>{to_html(_who(app_state, int(arg)) if arg.isdigit() else arg)}</b>?")
            + "\n\nЧеловек останется менеджером: спрашивать базу и вносить в неё сможет, "
              "управлять доступами — нет. Совсем убрать из бота — следом «➖ Менеджер».",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Снять", callback_data=f"m:ppl:rml2:{arg}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="m:do:people"),
            ]]),
        )
        return

    if action == "rml2":
        if not arg.isdigit():
            await callback.answer()
            return
        gone = int(arg)
        row = next((r for r in app_state.leaders if int(r.get("id") or 0) == gone), None)
        name = _leader_name(row, app_state) if row else ""
        ok = app_state.remove_leader(gone)
        if not ok:
            await callback.answer(
                "Не снял: это единственный руководитель или его уже нет в списке.",
                show_alert=True,
            )
            return
        # Снятый руководитель остаётся менеджером: роль не должна молча лишать базы.
        app_state.add_manager(actor.id, user_id=gone, name=row.get("name", "") if row else "")
        sync_leaders(config, app_state)
        await callback.answer("Снял")
        log.info("Руководитель %s снят пользователем %s", gone, actor.id)
        if gone != actor.id:
            await notify_manager(
                callback.bot, gone,
                "С тебя сняли роль руководителя базы знаний. Доступ менеджера остался.",
            )
        await _tell_other_leaders(
            callback, config,
            f"👥 Снят руководитель: {name}. Снял {actor.full_name}.",
        )
        if gone == actor.id:
            await _edit(
                callback, "Роль руководителя с тебя снята. Ты остался менеджером.",
                InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="☰ Меню", callback_data="m:nav:root"),
                ]]),
            )
            return
        text, markup = _people_view(config, app_state)
        await _edit(callback, text, markup)
        return

    if action == "req":
        await callback.answer()
        if not PENDING_ACCESS:
            await chat.answer("Заявок на доступ нет.")
            return
        for uid, (name, username) in list(PENDING_ACCESS.items())[:20]:
            who = f"@{username}" if username else name
            await chat.answer(
                f"🔑 <b>Просит доступ</b>: {to_html(name)}" + (f" ({to_html(who)})" if username else ""),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Дать доступ", callback_data=f"access:yes:{uid}"),
                    InlineKeyboardButton(text="❌ Отказать", callback_data=f"access:no:{uid}"),
                ]]),
            )
        return

    await callback.answer()


async def _tell_other_leaders(callback: CallbackQuery, config: Config, text: str) -> None:
    """Смена руководителей не проходит тихо: остальные узнают сразу."""
    for uid in sorted(config.leaders):
        if uid != callback.from_user.id:
            await notify_manager(callback.bot, uid, text)


def _label_by_key(app_state: State, key: str) -> str:
    """Подпись менеджера по ключу из кнопки (id или ник)."""
    for m in app_state.extra_managers:
        if _manager_key(m) == key:
            return _manager_label(m)
    return f"@{key}" if not key.isdigit() else (app_state.user_label(int(key)) or UNKNOWN_NAME)
