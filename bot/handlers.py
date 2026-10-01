"""Обработчики Telegram."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import date
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import doc_text
import kb as kb_module
import kb_write
import links as links_module
from autonomy import AutoWriter
import dedupe
import journal
import models_catalog
import nowblock
import period
import qtype
import uploads
from agent import Agent, Dialog
from dialogs import DialogStore
from answer_cache import AnswerCache
from chat_log import ChatLog, ChatMessage, describe_kind, topic_of
from config import Config
from files_lib import FileLibrary
from formatting import to_html
from kb import KnowledgeBase
from kb_editor import KbEditor
from links import LinkBook
from notes import FeedbackLog, SuggestionLog
from publisher import Publisher
from safety import as_data
from state import State
from access import deny as _deny, resolve_role, role_of as _role
# Импорт односторонний: h_edit ничего не знает про handlers, иначе кольцо.
import h_edit
import kbconfig
import h_menu
from h_edit import (
    on_pending_facts,
    run_update as _run_update,
)
from menu import (
    ADMIN_HELP,
    BTN_CHANGES,
    BTN_FILES,
    BTN_HELP,
    BTN_HYPOTHESES,
    BTN_PENDING,
    BTN_PROPOSE,
    BTN_SUGGEST,
    BTN_UPDATE,
    BTN_USERS,
    GREETING,
    LEGACY_LABELS,
    LINK_PREFIX as _LINK_PREFIX,
    MANAGER_HELP,
    MENU_LABELS,
    UPDATE_PREFIX as _UPDATE_PREFIX,
    menu_for as _menu_for,
    showcase as _showcase,
)
# Импортируем имена, а не модуль: объекты общие и мутабельные, копий быть не должно.
from botstate import (
    KB_WRITE_LOCK,
    CHAT_DRAFT_TTL,
    MORE_PENDING,
    MoreRequest,
    PENDING_ACCESS,
    PENDING_CHAT_DRAFTS,
    PENDING_FILE_META,
    PENDING_FORWARDS,
    PENDING_LINK,
    PENDING_PROPOSAL,
    PENDING_SUGGESTION,
    PENDING_UPDATE,
    PENDING_UPLOADS,
    PendingForward,
    QUESTION_BY_MSG,
    forget_pending_text as _forget_pending_text,
    is_cancel as _is_cancel,
    next_pending_id as _next_pending_id,
    user_lock as _user_lock,
)
from ui import (
    notify_leaders,
    answer_html as _answer_html,
    clear_markup as _clear_markup,
    clip_text as _clip_text,
    cut as _cut,
    keep_typing as _keep_typing,
    notify_manager as _notify_manager,
    send as _send,
    send_files as _send_files,
    send_long as _send_long,
    trim_dict as _trim_dict,
)
from uploads import PendingUpload
from usage import Transcript, UsageLog

log = logging.getLogger(__name__)
# Корневой роутер — агрегатор без собственных обработчиков: aiogram сначала прогоняет
# обработчики самого роутера и лишь потом вложенные, и «ловящий всё» `on_question`
# перехватывал бы команды вынесенных модулей. Всё из этого файла висит на `own`,
# а `own` включается последним.
router = Router()
own = Router()


# Отдельный раздел кэша для рабочих чатов: формат ответа там короче, чем в личке.
CHAT_SCOPE = "chat"

# Сколько последних сообщений топика подкладываем в рабочем чате. Больше — в запрос
# попадает чужой разговор, и модель отвечает на него.
CHAT_CONTEXT_MESSAGES = 10

# Окно для вопроса-обрывка («а если нет?»). Дальше в окно заходит соседняя тема.
CHAT_CONTEXT_DEEP = 25

# Типы ответа, под которыми есть кнопка «Подробнее»: только те форматы, что режут
# ответ до нескольких строк.
EXPANDABLE = {qtype.FACT, qtype.OBJECTION}


def _feedback_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Верно", callback_data="fb:ok"),
            InlineKeyboardButton(text="🤔 Спорно", callback_data="fb:maybe"),
            InlineKeyboardButton(text="❌ Неверно", callback_data="fb:no"),
        ]]
    )


def _answer_kb(more_id: int | None, feedback: bool) -> InlineKeyboardMarkup | None:
    """Кнопки под ответом: «Подробнее» и оценка; None, если ни одной не нужно."""
    rows: list[list[InlineKeyboardButton]] = []
    if more_id is not None:
        rows.append([
            InlineKeyboardButton(text="📖 Подробнее", callback_data=f"more:{more_id}")
        ])
    if feedback:
        rows.append(_feedback_kb().inline_keyboard[0])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_answer(
    message: Message,
    text: str,
    question: str,
    model: str,
    kind: str,
    units: list[str],
    in_group: bool = False,
) -> None:
    """Единая точка отправки ответа по базе: «Подробнее» — только под коротким форматом
    с известными единицами, оценка — только в личке."""
    more_id: int | None = None
    if kind in EXPANDABLE and units:
        more_id = _next_pending_id()
        MORE_PENDING[more_id] = MoreRequest(
            question=question, units=list(units), model=model, in_group=in_group
        )
        _trim_dict(MORE_PENDING, limit=200)
    sent = await _send(message, text, markup=_answer_kb(more_id, feedback=not in_group))
    if sent is not None and not in_group:
        QUESTION_BY_MSG[(message.chat.id, sent.message_id)] = (question, model)
        _trim_dict(QUESTION_BY_MSG)


@own.callback_query(F.data.startswith("access:"))
async def on_access_decision(
    callback: CallbackQuery, config: Config, app_state: State
) -> None:
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":", 2)
    await _clear_markup(callback)
    try:
        user_id = int(raw_id)
    except ValueError:
        await callback.answer("Заявка неактуальна")
        return
    name, username = PENDING_ACCESS.pop(user_id, ("", ""))
    who = f"@{username}" if username else (name or str(user_id))

    if action == "no":
        await callback.answer("Отказано")
        await callback.message.reply(f"Отказал. {who} доступа не получил.")
        return

    added = app_state.add_manager(
        callback.from_user.id, user_id=user_id, username=username or None, name=name
    )
    await callback.answer("Доступ выдан")
    await callback.message.reply(
        f"✅ {who} теперь менеджер — может спрашивать базу."
        if added else f"{who} уже был в списке."
    )
    await _notify_manager(
        callback.bot, user_id,
        f"Доступ открыт! Спрашивай своими словами — например «{kbconfig.CFG.example('question')}» "
        "или «у клиента просели показатели». Начни с /start, чтобы увидеть меню.",
    )


@own.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def on_start(message: Message, config: Config, app_state: State) -> None:
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return
    user = message.from_user
    if role == "leader":
        app_state.name_leader(user.id, user.full_name)
    app_state.note_manager_id(user.id, user.username, user.full_name)
    app_state.note_user(user.id, user.username, user.full_name)
    # /start сбрасывает и режим ввода: иначе следующий вопрос ушёл бы в редактор базы.
    _forget_pending_text(user.id)
    # Два сообщения: у сообщения бывает только одна клавиатура — reply-меню и inline-меню отдельно.
    await message.answer(
        GREETING + (ADMIN_HELP if role == "leader" else MANAGER_HELP),
        parse_mode="HTML",
        reply_markup=_menu_for(role),
    )
    await h_menu.send_menu(message, role)


@own.message(Command("reset"), F.chat.type == ChatType.PRIVATE)
async def on_reset(
    message: Message, config: Config, app_state: State, dialog_store: DialogStore
) -> None:
    if not _role(config, message, app_state):
        await _deny(message, config)
        return
    dialog_store.reset(message.from_user.id)
    dropped = _forget_pending_text(message.from_user.id)
    await message.answer(
        "Разговор сброшен. Спрашивай заново."
        + (" Режим ввода тоже отменил." if dropped else "")
    )


@own.message(Command("whoami"), F.chat.type == ChatType.PRIVATE)
async def on_whoami(message: Message, config: Config, app_state: State) -> None:
    user = message.from_user
    await message.answer(
        f"Твой Telegram ID: {user.id}\n"
        f"Ник: @{user.username or 'нет'}\n"
        f"Роль: {resolve_role(config, app_state, user.id, user.username) or 'нет доступа'}"
    )


@own.message(Command("users"), F.chat.type == ChatType.PRIVATE)
async def on_users(message: Message, config: Config, app_state: State) -> None:
    if _role(config, message, app_state) != "leader":
        return
    text, markup = h_menu._people_view(config, app_state)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@own.message(Command("add"), F.chat.type == ChatType.PRIVATE)
async def on_add(message: Message, config: Config, app_state: State) -> None:
    if _role(config, message, app_state) != "leader":
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg:
        await message.answer(
            "Добавить менеджера: <code>/add @ник</code> или <code>/add 12345678</code> (id).\n"
            "Свой id и ник человек узнаёт командой /whoami в этом боте.",
            parse_mode="HTML",
        )
        return
    if arg.lstrip("@").isdigit():
        ok = app_state.add_manager(message.from_user.id, user_id=int(arg.lstrip("@")))
        who = arg
    else:
        ok = app_state.add_manager(message.from_user.id, username=arg)
        who = "@" + arg.lstrip("@")
    await message.answer(
        f"{who} добавлен как менеджер." if ok else f"{who} уже в списке."
    )


@own.message(Command("remove"), F.chat.type == ChatType.PRIVATE)
async def on_remove(message: Message, config: Config, app_state: State) -> None:
    if _role(config, message, app_state) != "leader":
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg:
        await message.answer("Укажи кого убрать: <code>/remove @ник</code> или <code>/remove id</code>.", parse_mode="HTML")
        return
    ok = app_state.remove_manager(arg)
    await message.answer(
        f"{arg} убран из менеджеров." if ok else
        f"{arg} не найден среди добавленных через бота (в .env убирается вручную)."
    )


@own.message(Command("model"), F.chat.type == ChatType.PRIVATE)
async def on_model(message: Message, config: Config, app_state: State) -> None:
    if _role(config, message) != "leader":
        await message.answer("Переключать модель может только руководитель.")
        return

    prices = await models_catalog.fetch_prices()
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if m.id == app_state.model else "") + m.label,
                callback_data=f"setmodel:{m.id}",
            )
        ]
        for m in models_catalog.CATALOG
    ]
    rows.append(
        [InlineKeyboardButton(text="📋 Показать все доступные", callback_data="allmodels")]
    )
    await message.answer(
        models_catalog.format_catalog(app_state.model, prices),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@own.callback_query(F.data == "allmodels")
async def on_all_models(callback: CallbackQuery, config: Config, app_state: State) -> None:
    """Живой список моделей из OpenRouter."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return

    await callback.answer("Спрашиваю OpenRouter…")
    models = await models_catalog.fetch_live_catalog()
    cheapest = sorted(models.values(), key=models_catalog.estimate_question_cost)[:12]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ " if m["id"] == app_state.model else "") + m["id"],
                    callback_data=f"setmodel:{m['id']}",
                )
            ]
            for m in cheapest
            # callback_data ограничен 64 байтами — длинные id кнопкой не переключить.
            if len(f"setmodel:{m['id']}".encode()) <= 64
        ]
    )
    await callback.message.answer(
        models_catalog.format_live(app_state.model, models),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@own.callback_query(F.data.startswith("setmodel:"))
async def on_set_model(callback: CallbackQuery, config: Config, app_state: State) -> None:
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return

    model_id = callback.data.split(":", 1)[1]
    if not models_catalog.is_known(model_id):
        await callback.answer("Неизвестная модель", show_alert=True)
        return

    app_state.model = model_id
    known = models_catalog.BY_ID.get(model_id)
    label = known.label if known else model_id
    await callback.answer(f"Переключено на {label}")
    await callback.message.answer(
        f"Модель переключена на <b>{label}</b>.\n"
        f"Действует со следующего вопроса, перезапуск не нужен.\n\n"
        f"Сравнить расход потом — /stats",
        parse_mode="HTML",
    )
    log.info("Руководитель %s переключил модель на %s", callback.from_user.id, model_id)


@own.message(Command("stats"), F.chat.type == ChatType.PRIVATE)
async def on_stats(
    message: Message, config: Config, usage_log: UsageLog, answer_cache: AnswerCache
) -> None:
    if _role(config, message) != "leader":
        await message.answer("Статистика доступна руководителю.")
        return
    await _answer_html(message, usage_log.summary(days=30) + "\n\n" + answer_cache.summary())


@own.message(Command("otzyvy"), F.chat.type == ChatType.PRIVATE)
async def on_otzyvy(message: Message, config: Config, feedback_log: FeedbackLog) -> None:
    if _role(config, message) != "leader":
        return
    await _answer_html(message, feedback_log.summary())


@own.message(Command("idei"), F.chat.type == ChatType.PRIVATE)
async def on_idei(message: Message, config: Config, suggestion_log: SuggestionLog) -> None:
    if _role(config, message) != "leader":
        return
    await _answer_html(message, suggestion_log.summary())


@own.callback_query(F.data.startswith("fb:"))
async def on_feedback(
    callback: CallbackQuery, config: Config, feedback_log: FeedbackLog,
    app_state: State, answer_cache: AnswerCache,
) -> None:
    # resolve_role, а не config.role: менеджеры, добавленные через /add, живут в state.
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer()
        return
    rating = callback.data.split(":", 1)[1]
    question, answered_by = (
        QUESTION_BY_MSG.get(
            (callback.message.chat.id, callback.message.message_id), ("", app_state.model)
        )
        if callback.message else ("", app_state.model)
    )
    feedback_log.record(callback.from_user.id, callback.from_user.full_name, rating, question)
    # Неверный ответ выбрасываем из кэша — по той модели, которой отвечали.
    if rating == "no" and question:
        answer_cache.drop(question, answered_by)
    thanks = {"ok": "Спасибо! Рад, что помог.", "maybe": "Понял, помечу как спорное.",
              "no": "Спасибо, зафиксировал — разберём."}.get(rating, "Спасибо!")
    await callback.answer(thanks)
    try:
        mark = {"ok": "✅ оценено: верно", "maybe": "🤔 оценено: спорно", "no": "❌ оценено: неверно"}[rating]
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(mark)
    except Exception:
        pass


@own.callback_query(F.data.startswith("more:"))
async def on_more(
    callback: CallbackQuery, config: Config, app_state: State, agent: Agent,
    usage_log: UsageLog,
) -> None:
    """«Подробнее» под коротким ответом: второй ответ по тем же единицам, считается
    только по нажатию. Доступ — как к самому ответу: в личке роль, в чате — сам чат."""
    message = callback.message
    if message is None:
        await callback.answer()
        return
    in_private = message.chat.type == ChatType.PRIVATE
    if in_private:
        if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
            await callback.answer()
            return
    else:
        thread_id = message.message_thread_id or 0
        if not app_state.is_listening(message.chat.id, thread_id):
            await callback.answer()
            return

    try:
        key = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    # Забираем сразу: повторное нажатие не должно списывать второй разворот.
    request = MORE_PENDING.pop(key, None)
    if request is None:
        await callback.answer("Эта кнопка уже неактуальна — спроси заново.", show_alert=True)
        return

    await callback.answer("Разворачиваю…")
    try:
        await message.edit_reply_markup(
            reply_markup=_feedback_kb() if in_private else None
        )
    except Exception:
        log.info("Не смог убрать кнопку «Подробнее» — продолжаю", exc_info=True)

    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        result = await agent.expand(
            request.question, request.units,
            model=request.model, in_group=request.in_group,
        )
    except Exception:
        log.exception("Не смог развернуть ответ по %s", request.units)
        MORE_PENDING[key] = request  # не списали — пусть кнопка ещё сработает
        await message.reply("Не смог развернуть ответ. Попробуй нажать ещё раз.")
        return
    finally:
        typing.cancel()

    usage_log.record(callback.from_user.id, request.model, result.usage, 0.0)
    log.info(
        "«Подробнее» для %s | единицы %s | %s",
        callback.from_user.id, ", ".join(result.units) or "нет", result.usage,
    )
    # Развёрнутый ответ в кэш не кладём: из кэша он приехал бы вместо короткого.
    await _send(message, result.text)
    await _send_files(message, result.files)


# Сколько моделей спрашиваем за один /compare: текущая плюс следующие по списку.
COMPARE_LIMIT = 3


@own.message(Command("compare"), F.chat.type == ChatType.PRIVATE)
async def on_compare(message: Message, config: Config, agent: Agent, app_state: State) -> None:
    if _role(config, message) != "leader":
        await message.answer("Сравнение доступно руководителю.")
        return

    question = (message.text or "").partition(" ")[2].strip()
    if not question:
        await message.answer(
            "Напиши вопрос после команды, например:\n"
            "<code>/compare у клиента просели позиции, что делать</code>",
            parse_mode="HTML",
        )
        return

    order = [app_state.model] + [m.id for m in models_catalog.CATALOG if m.id != app_state.model]
    chosen = order[:COMPARE_LIMIT]

    await message.answer(
        "Спрашиваю у моделей: "
        + ", ".join(models_catalog.label_for(m) for m in chosen)
        + ".\nЭто займёт с полминуты."
    )

    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        results = await asyncio.gather(
            *(_ask_one(agent, question, model_id) for model_id in chosen),
            return_exceptions=True,
        )
    finally:
        typing.cancel()

    for model_id, result in zip(chosen, results):
        label = models_catalog.label_for(model_id)
        if isinstance(result, Exception):
            log.warning("Сравнение: %s упала", model_id, exc_info=result)
            await message.answer(f"<b>{label}</b>\nОшибка: {result}", parse_mode="HTML")
            continue
        answer, usage, seconds = result
        head = (
            f"<b>{label}</b> — ${usage.cost:.4f}, {seconds:.0f} сек, "
            f"из кэша {usage.cached_tokens} токенов"
        )
        await _send(message, f"{head}\n\n{answer}", already_html_head=True)


async def _ask_one(agent: Agent, question: str, model_id: str):
    """Отдельный диалог на модель: истории не должны смешиваться."""
    started = time.monotonic()
    result = await agent.answer(Dialog(), question, model=model_id)
    return result.text, result.usage, time.monotonic() - started


@own.message(Command("izmeneniya"), F.chat.type == ChatType.PRIVATE)
async def on_period_report(
    message: Message, config: Config, app_state: State, kb: KnowledgeBase,
    kb_editor: KbEditor,
) -> None:
    """«Что изменилось за N дней»: проекция по git-истории knowledge/, горизонтам единиц и журналу."""
    if not _role(config, message, app_state):
        await _deny(message, config)
        return
    days = 7
    arg = (message.text or "").partition(" ")[2].strip()
    if arg.isdigit() and 1 <= int(arg) <= 90:
        days = int(arg)
    await _send_period_report(message, app_state, kb, kb_editor, days)


async def _send_period_report(
    message: Message, app_state: State, kb: KnowledgeBase, kb_editor: KbEditor, days: int
) -> None:
    report = period.collect(
        kb.root, kb, days=days, pending=len(app_state.pending_digest_facts())
    )
    story = ""
    if not report.is_empty:
        typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
        try:
            story = await kb_editor.summarize_period(
                period.SUMMARY_SYSTEM, period.summary_prompt(report), model=app_state.model
            )
        except Exception:
            log.warning("Пересказ сводки не собрался — отдаю по фактам", exc_info=True)
        finally:
            typing.cancel()
    # Пересказ уже размечен под Telegram — экранировать нельзя.
    await _send_long(message.bot, message.chat.id, period.render(report, story), _period_kb(days))


def _period_kb(days: int) -> InlineKeyboardMarkup:
    """Кнопки под сводкой: переключить окно и оценить."""
    other = 14 if days == 7 else 7
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 За {other} дней", callback_data=f"period:{other}")],
        [
            InlineKeyboardButton(text="✅ Ок", callback_data=f"pfb:ok:{days}"),
            InlineKeyboardButton(text="🤔 Спорно", callback_data=f"pfb:maybe:{days}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"pfb:no:{days}"),
        ],
    ])


@own.callback_query(F.data.startswith("pfb:"))
async def on_period_feedback(
    callback: CallbackQuery, config: Config, app_state: State, feedback_log: FeedbackLog
) -> None:
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer()
        return
    parts = callback.data.split(":")
    rating = parts[1] if len(parts) > 1 else "?"
    days = parts[2] if len(parts) > 2 else "?"
    feedback_log.record(
        callback.from_user.id, callback.from_user.full_name, rating,
        f"[сводка за {days} дней]",
    )
    thanks = {
        "ok": "Спасибо — значит, формат рабочий.",
        "maybe": "Понял, помечу как спорное. Скажи, что не так — поправлю.",
        "no": "Записал. Что именно мимо? Так я пойму, что чинить.",
    }.get(rating, "Спасибо!")
    await callback.answer(thanks, show_alert=rating != "ok")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="📅 За 14 дней" if days == "7" else "📅 За 7 дней",
                    callback_data=f"period:{14 if days == '7' else 7}",
                )
            ]])
        )
    except Exception:
        log.info("Не смог обновить кнопки под сводкой", exc_info=True)


@own.callback_query(F.data.startswith("period:"))
async def on_period_switch(
    callback: CallbackQuery, config: Config, app_state: State, kb: KnowledgeBase,
    kb_editor: KbEditor,
) -> None:
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer()
        return
    try:
        days = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    await callback.answer(f"Собираю за {days} дней…")
    await _send_period_report(callback.message, app_state, kb, kb_editor, days)


SECTION_TITLES = kbconfig.CFG.section_titles


@own.message(Command("sostoyanie"), F.chat.type == ChatType.PRIVATE)
async def on_state_map(
    message: Message, config: Config, app_state: State, kb: KnowledgeBase
) -> None:
    """Карта состояния: строки блока «Сейчас» всех единиц одним экраном, по разделам базы."""
    if not _role(config, message, app_state):
        await _deny(message, config)
        return
    groups = nowblock.state_map(kb.units.values(), days=STATE_FRESH_DAYS)
    if not groups:
        await message.answer(
            "Ни у одной единицы нет блока «Сейчас» — карту собирать не из чего."
        )
        return

    total = sum(len(rows) for _s, rows in groups)
    fresh = sum(1 for _s, rows in groups for _id, _e, is_fresh in rows if is_fresh)
    out = [
        f"🗺 <b>Карта состояния</b> — {total} строк(и) в {len(groups)} категориях",
        f"<i>🆕 — менялось за последние {STATE_FRESH_DAYS} дней: {fresh}</i>",
        "",
    ]
    for section, rows in groups:
        out.append(f"<b>{SECTION_TITLES.get(section, section)}</b>")
        for _kb_id, entry, is_fresh in rows:
            mark = "🆕 " if is_fresh else ""
            since = f" <i>(с {entry.since})</i>" if entry.since else ""
            out.append(f"• {mark}<b>{to_html(entry.key)}:</b> {to_html(entry.value)}{since}")
        out.append("")
    out.append(
        "<i>Это не отдельная страница, а срез по базе: правится обычной правкой "
        "нужной единицы, и карта меняется сама.</i>"
    )
    await _answer_html(message, "\n".join(out))


# Что считаем «свежим» в карте состояния и в ответах.
STATE_FRESH_DAYS = 14


@own.message(Command("voprosy"), F.chat.type == ChatType.PRIVATE)
async def on_top_questions(
    message: Message, config: Config, app_state: State, transcript: Transcript
) -> None:
    """Топ вопросов по журналу ответов; одинаковые по смыслу вопросы схлопнуты."""
    if _role(config, message, app_state) != "leader":
        await _deny(message, config)
        return
    rows = await asyncio.to_thread(transcript.top_questions, 30, 15)
    if not rows:
        await message.answer("Вопросов пока не набралось — журнал пуст.")
        return
    out = ["📊 <b>О чём спрашивают чаще всего</b> (за 30 дней)", ""]
    for question, count, kinds in rows:
        kind_note = f" · {', '.join(kinds)}" if kinds else ""
        out.append(f"• <b>{count}×</b> {to_html(question[:110])}{kind_note}")
    out.append("")
    out.append(
        "<i>Отсюда растёт эталонный набор: правки промптов проверяются в первую "
        "очередь на том, что спрашивают каждый день.</i>"
    )
    await _answer_html(message, "\n".join(out))


@own.message(Command("jurnal"), F.chat.type == ChatType.PRIVATE)
async def on_journal(
    message: Message, config: Config, app_state: State, kb: KnowledgeBase,
    chat_log: ChatLog,
) -> None:
    """Журнал событий: что ломалось за период и сколько дней тянулось."""
    if not _role(config, message, app_state):
        await _deny(message, config)
        return
    arg = (message.text or "").partition(" ")[2].strip()
    days = int(arg) if arg.isdigit() and 0 < int(arg) <= 90 else 30
    messages = await asyncio.to_thread(chat_log.recent_all, days)
    events = journal.collect(messages, days=days)
    incidents = kb.by_horizon(kb_module.HORIZON_INCIDENT)
    await _answer_html(message, journal.render(events, incidents, days))


@own.message(Command("gipotezy"), F.chat.type == ChatType.PRIVATE)
async def on_hypotheses(
    message: Message, config: Config, app_state: State, kb: KnowledgeBase
) -> None:
    """Открытые гипотезы: эксперименты, у которых не подведён итог."""
    if not _role(config, message, app_state):
        await _deny(message, config)
        return
    await _answer_html(message, _hypotheses_text(kb))


def _hypotheses_text(kb: KnowledgeBase, today: str = "") -> str:
    today = today or date.today().isoformat()
    open_units = kb.open_experiments(today)
    closed = [u for u in kb.by_horizon(kb_module.HORIZON_EXPERIMENT) if u.closed]
    if not open_units and not closed:
        return (
            "Гипотез в базе не помечено.\n\n"
            "Признак ставится в шапке единицы: <code>horizon: experiment</code> "
            "плюс <code>control_point</code> (когда подводим итог) и "
            "<code>closed</code> (когда подвели)."
        )
    lines = []
    if open_units:
        lines.append(f"<b>Открытые гипотезы</b> ({len(open_units)}) — итог не подведён")
        for unit in open_units:
            overdue = unit.control_point and unit.control_point <= today
            mark = "🔴 срок прошёл" if overdue else f"⏳ до {unit.control_point or 'без срока'}"
            lines.append(f"• <b>{unit.id}</b> · {to_html(unit.title[:70])}\n   {mark}")
        lines.append("")
        lines.append(
            "<i>Закрыть — обычной правкой базы: «обнови базу: по kb-104 итог такой-то». "
            "Я допишу вывод и поставлю дату закрытия.</i>"
        )
    if closed:
        lines.append("")
        lines.append(f"<b>Закрытые</b> ({len(closed)}):")
        for unit in closed:
            lines.append(f"• {unit.id} · {to_html(unit.title[:70])} — закрыта {unit.closed}")
    return "\n".join(lines)


@own.message(Command("links"), F.chat.type == ChatType.PRIVATE)
async def on_links(message: Message, config: Config, app_state: State, links: LinkBook) -> None:
    """Список полезных ссылок. Видят все, правит только руководитель."""
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return
    keyboard = None
    if role == "leader" and links.links:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить ссылку…", callback_data="link:delmenu")
        ]])
    await message.answer(
        links.summary(), parse_mode="HTML", reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@own.callback_query(F.data == "link:delmenu")
async def on_link_delmenu(
    callback: CallbackQuery, config: Config, links: LinkBook
) -> None:
    """Удаление ссылки, шаг 1: выбрать какую."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    if not links.links:
        await callback.answer("Ссылок нет")
        return
    rows = [
        [InlineKeyboardButton(text=link.title[:56], callback_data=f"link:ask:{link.key}")]
        for link in links.links[:40]
    ]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="link:cancel")])
    await callback.message.edit_text(
        "Какую ссылку удалить? Нажатие здесь ещё НЕ удаляет — спрошу подтверждение.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@own.callback_query(F.data.startswith("link:ask:"))
async def on_link_ask(
    callback: CallbackQuery, config: Config, links: LinkBook
) -> None:
    """Удаление ссылки, шаг 2: подтверждение с названием и адресом."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    link = links.get(key)
    if link is None:
        await callback.answer("Ссылка уже удалена")
        return
    await callback.message.edit_text(
        f"Удалить «{to_html(link.title)}»?\n{link.url}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"link:del:{key}"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="link:cancel"),
        ]]),
    )
    await callback.answer()


@own.callback_query(F.data == "link:cancel")
async def on_link_cancel(
    callback: CallbackQuery, config: Config, links: LinkBook
) -> None:
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    await callback.message.edit_text(
        links.summary(), parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить ссылку…", callback_data="link:delmenu")
        ]]),
    )
    await callback.answer("Ничего не удалил")


@own.callback_query(F.data.startswith("link:del:"))
async def on_link_delete(
    callback: CallbackQuery, config: Config, links: LinkBook, publisher: Publisher
) -> None:
    """Удаление ссылки, шаг 3: собственно удаление после явного подтверждения."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    link = await asyncio.to_thread(links.remove, key)
    if link is None:
        await callback.answer("Ссылка уже удалена")
        return
    await callback.answer(f"Удалил «{link.title}»")
    ok, msg = await asyncio.to_thread(
        publisher.commit_paths, [links_module.REL_PATH],
        f"Ссылки (бот): убрал «{link.title}», подтвердил {callback.from_user.id}",
    )
    await callback.message.edit_text(
        f"Удалил ссылку «{to_html(link.title)}». Вернуть — пришли её заново "
        f"(«запомни ссылку …»)." + ("" if ok else f"\n⚠️ В git не ушло: {msg}"),
        parse_mode="HTML",
    )


async def _save_link(
    message: Message, links: LinkBook, publisher: Publisher, payload: str, author: str
) -> None:
    """Сохраняет ссылку и сразу коммитит: ссылки нужны менеджерам немедленно."""
    parsed = links_module.parse_command(payload)
    if parsed is None:
        await message.answer(
            "Не нашёл в сообщении ссылку. Напиши так:\n"
            "<code>запомни ссылку https://zoom.us/j/123 — зум для планёрок</code>",
            parse_mode="HTML",
        )
        return
    title, url, tags = parsed
    ok, note = await asyncio.to_thread(links.add, title, url, tags, author)
    if not ok:
        await message.answer(note)
        return
    git_ok, git_msg = await asyncio.to_thread(
        publisher.commit_paths, [links_module.REL_PATH],
        f"Ссылки (бот): {title or url}, добавил {message.from_user.id}",
    )
    await message.answer(
        f"{note}\nМенеджеры уже могут спросить её словами — например «дай ссылку на "
        f"{(tags[0] if tags else title or 'это')}».\nСписок — /links."
        + ("" if git_ok else f"\n\n⚠️ В git не ушло: {git_msg}")
    )


@own.message(Command("publish"), F.chat.type == ChatType.PRIVATE)
async def on_publish(message: Message, config: Config, publisher: Publisher) -> None:
    if _role(config, message) != "leader":
        await message.answer("Публиковать файлы может только руководитель.")
        return
    await message.answer("Публикую загруженные файлы в git…")
    ok, text = await asyncio.to_thread(publisher.publish, "по кнопке")
    await message.answer(text if ok else f"Не вышло: {text}")


def _forward_origin(message: Message) -> str:
    """Откуда переслали — только то, что Telegram сообщил, без догадок (идёт в sources и в коммит)."""
    origin = message.forward_origin
    kind = getattr(origin, "type", None)
    if kind == "user":
        user = origin.sender_user
        who = f"@{user.username}" if user.username else user.full_name
        return f"переслано из личного сообщения, автор {who}"
    if kind == "channel":
        title = getattr(origin.chat, "title", None) or "канал без названия"
        return f"переслано из канала «{title}», сообщение #{origin.message_id}"
    if kind == "chat":
        title = getattr(origin.sender_chat, "title", None) or "чат без названия"
        return f"переслано из чата «{title}»"
    if kind == "hidden_user":
        return f"переслано, автор скрыт ({getattr(origin, 'sender_user_name', 'без имени')})"
    return "переслано в бота"


@own.message(F.forward_origin, F.chat.type == ChatType.PRIVATE)
async def on_forward(message: Message, config: Config, app_state: State, files: FileLibrary) -> None:
    """Пересланное сообщение: спрашиваем, что с ним делать; форвард с документом идёт в библиотеку."""
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return
    if message.document:
        await on_document(message, config, files, app_state)
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer(
            "В пересланном сообщении нет текста — понимаю только текст. "
            "Если это файл для библиотеки, перешли его документом."
        )
        return

    origin = _forward_origin(message)
    PENDING_FORWARDS[message.from_user.id] = PendingForward(text=text, origin=origin)
    _trim_dict(PENDING_FORWARDS, limit=200)
    to_base = "✏️ В базу"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=to_base, callback_data="fwd:base")],
        [
            InlineKeyboardButton(text="💡 Как идею", callback_data="fwd:idea"),
            InlineKeyboardButton(text="❌ Ничего", callback_data="fwd:no"),
        ],
    ])
    await message.answer(
        f"Принял: <i>{to_html(origin)}</i>.\nЧто с этим сделать?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@own.callback_query(F.data.startswith("fwd:"))
async def on_forward_decision(
    callback: CallbackQuery, config: Config, app_state: State,
    kb_editor: KbEditor, suggestion_log: SuggestionLog,
) -> None:
    role = resolve_role(config, app_state, callback.from_user.id, callback.from_user.username)
    if not role:
        await callback.answer("Нет доступа", show_alert=True)
        return
    pending = PENDING_FORWARDS.pop(callback.from_user.id, None)
    await _clear_markup(callback)
    if pending is None:
        await callback.answer("Сообщение уже неактуально")
        return
    action = callback.data.split(":", 1)[1]

    if action == "no":
        await callback.answer("Ок")
        await callback.message.reply("Ничего не делаю.")
        return

    if action == "idea":
        suggestion_log.record(
            callback.from_user.id, callback.from_user.full_name,
            f"[{pending.origin}] {pending.text}",
        )
        await callback.answer("Записал в идеи")
        await callback.message.reply("Записал в предложения — обсудим.")
        return

    # action == "base": путь один для всех ролей — бот показывает формулировку, автор подтверждает.
    await callback.answer("Готовлю правку…")
    await _run_update(
        callback.bot, callback.message.chat.id, callback.from_user.id,
        app_state, kb_editor, pending.text, origin=pending.origin,
        author=callback.from_user.full_name, leader=(role == "leader"),
    )


@own.callback_query(F.data.startswith("doc:"))
async def on_document_choice(
    callback: CallbackQuery, config: Config, app_state: State, kb_editor: KbEditor
) -> None:
    """Текстовый файл: в библиотеку как материал или его текст — в базу знаний."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    await _clear_markup(callback)
    pending_text = PENDING_FORWARDS.pop(callback.from_user.id, None)

    if callback.data.endswith("digest"):
        # Разбор документа: только показываем, что в нём нового; в базу не пишем.
        if pending_text is None:
            await callback.answer("Файл уже неактуален")
            return
        # Файл снимаем с очереди на загрузку, иначе следующая фраза станет его описанием.
        upload = PENDING_UPLOADS.pop(callback.from_user.id, None)
        if upload is not None:
            upload.staged.unlink(missing_ok=True)
        await callback.answer("Читаю файл…")
        await callback.message.reply("Читаю файл и собираю разбор…")
        typing = asyncio.create_task(_keep_typing(callback.bot, callback.message.chat.id))
        try:
            digest = await kb_editor.review_document(
                pending_text.origin, pending_text.text, model=app_state.model
            )
        except Exception:
            log.exception("Не смог разобрать документ")
            await callback.message.reply("Не смог разобрать файл — попробуй ещё раз.")
            return
        finally:
            typing.cancel()
        await _send(callback.message, digest)
        await callback.message.reply(
            "Это разбор, в базу ничего не записано. Что из этого внести — пришли отдельно "
            "(«обнови базу: …»), покажу diff и спрошу подтверждение.\n"
            "Сам файл ещё можно положить в библиотеку — пришли его снова."
        )
        return

    if callback.data.endswith("lib"):
        await callback.answer("В библиотеку")
        await callback.message.reply(
            "Опиши файл одним сообщением:\n"
            "• строка 1 — <b>название</b>\n"
            "• строка 2 — <b>теги</b> через запятую\n"
            "• дальше — пара слов, о чём файл\n\n"
            "Или напиши «отмена».",
            parse_mode="HTML",
        )
        return

    # Текст — в базу, сам файл в библиотеку не берём.
    upload = PENDING_UPLOADS.pop(callback.from_user.id, None)
    if upload is not None:
        upload.staged.unlink(missing_ok=True)
    if pending_text is None:
        await callback.answer("Файл уже неактуален")
        return
    await callback.answer("Готовлю правку…")
    await _run_update(
        callback.bot, callback.message.chat.id, callback.from_user.id,
        app_state, kb_editor, pending_text.text, origin=pending_text.origin,
        author=callback.from_user.full_name, leader=True,
    )


@own.message(F.document, F.chat.type == ChatType.PRIVATE)
async def on_document(
    message: Message, config: Config, files: FileLibrary, app_state: State
) -> None:
    """Файл в личку: сохраняем и просим описание. Фильтр по личке стоит в декораторе:
    иначе обработчик перехватывал бы документы из рабочих чатов."""
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return
    simple = role != "leader"

    doc = message.document
    if doc.file_size and doc.file_size > uploads.MAX_FILE_MB * 1024 * 1024:
        await message.answer(
            f"Файл больше {uploads.MAX_FILE_MB} МБ — Telegram не даёт боту такие скачивать. "
            f"Пришли документ поменьше или сжатую версию."
        )
        return

    staging = files.dir / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    ext = Path(doc.file_name or "").suffix or ".bin"
    # Имя уникальное: второй файл подряд не должен затереть первый, пока тот ждёт описания.
    staged = staging / f"{message.from_user.id}-{uuid.uuid4().hex[:6]}{ext}"
    try:
        await message.bot.download(doc, destination=str(staged))
    except Exception:
        log.exception("Не смог скачать файл от %s", message.from_user.id)
        await message.answer("Не смог скачать файл. Попробуй ещё раз.")
        return

    PENDING_UPLOADS[message.from_user.id] = PendingUpload(
        staged=staged, original_name=doc.file_name or "файл", ext=ext,
        added_by=message.from_user.full_name, simple=simple,
    )

    if simple:
        await message.answer(
            f"Файл получил: <b>{to_html(doc.file_name or 'без имени')}</b>\n\n"
            f"Напиши одним сообщением, <b>что это и кому пригодится</b> — первая строка "
            f"станет названием. Сразу положу в библиотеку, с твоим именем и датой. "
            f"Передумал — «отмена».",
            parse_mode="HTML",
        )
        return

    if doc_text.can_extract(ext):
        text, note = await asyncio.to_thread(doc_text.extract, staged, ext)
        if text:
            PENDING_FORWARDS[message.from_user.id] = PendingForward(
                text=text, origin=f"из файла «{doc.file_name or 'без имени'}»"
            )
            caveat = f"\n⚠️ {note}" if note else ""
            await message.answer(
                f"Файл получил: <b>{doc.file_name or 'без имени'}</b>\n"
                f"Текста разобрал: {len(text)} символов.{caveat}\n\nЧто с ним сделать?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Разобрать по пунктам", callback_data="doc:digest")],
                    [InlineKeyboardButton(text="✏️ Текст — в базу", callback_data="doc:base")],
                    [InlineKeyboardButton(text="📎 В библиотеку", callback_data="doc:lib")],
                ]),
            )
            return
        if note:
            await message.answer(f"Текст из файла достать не смог: {note}.")

    await message.answer(
        f"Файл получил: <b>{doc.file_name or 'без имени'}</b>.\n\n"
        f"Опиши его одним сообщением:\n"
        f"• строка 1 — <b>название</b> (как менеджер его увидит)\n"
        f"• строка 2 — <b>теги</b> через запятую (для поиска)\n"
        f"• дальше — пара слов, о чём файл\n\n"
        f"Пример:\n"
        f"<code>Презентация «Карты» для клиентов\n"
        f"карты, презентация, продажи\n"
        f"Официальная презентация «Карты»: услуга, кейсы, тарифы.</code>\n\n"
        f"Или напиши «отмена».",
        parse_mode="HTML",
    )


GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

# Все личные команды ограничены личкой (фильтр в декораторе каждой): бот сам вступает
# в любой чат, и без фильтра `/start` в общем чате печатал бы Telegram ID, а `/users` —
# список людей отдела. В группах остаются только /listen и /unlisten.

# Границы сводки: по чатам, по сообщениям и по символам (режем по границе строки).
SVODKA_CHAT_LIMIT = 5
SVODKA_MSG_LIMIT = 300
SVODKA_MAX_CHARS = 60000

# Слово-аргумент к /listen и /unlisten, означающее «весь чат, а не этот топик».
WHOLE_CHAT_WORDS = {"чат", "весь", "всё", "все", "chat", "all"}


def _whole_chat_asked(message: Message) -> bool:
    parts = (message.text or message.caption or "").split(maxsplit=1)
    return len(parts) > 1 and parts[1].strip().lower().strip(".,!") in WHOLE_CHAT_WORDS


@own.message(Command("listen"), F.chat.type.in_(GROUP_TYPES))
async def on_listen(message: Message, config: Config, app_state: State) -> None:
    """Вернуть запись — в этом топике или во всём чате."""
    if _role(config, message, app_state) != "leader":
        return  # в группе на чужие команды не отвечаем, чтобы не шуметь
    app_state.note_chat(message.chat.id, message.chat.title or str(message.chat.id))
    thread_id, topic_name = topic_of(message)
    if topic_name:
        app_state.note_topic(message.chat.id, thread_id, topic_name)

    if thread_id and not _whole_chat_asked(message):
        name = topic_name or app_state.topic_name(message.chat.id, thread_id) or "этот топик"
        if app_state.unmute_topic(message.chat.id, thread_id):
            await message.reply(f"👂 Снова записываю топик «{name}».")
        elif not app_state.is_listening(message.chat.id):
            await message.reply(
                "Топик не заглушён, но заглушён весь чат — верни его командой «/listen чат»."
            )
        else:
            await message.reply(
                "Я и так записываю этот топик. Выключить запись здесь — /unlisten, "
                "во всём чате — «/unlisten чат»."
            )
        return

    if app_state.unmute_chat(message.chat.id):
        muted = app_state.muted_topics(message.chat.id)
        tail = (
            f"\nОтдельно заглушены топики: {', '.join(muted.values())} — вернуть можно "
            "командой /listen внутри каждого."
            if muted else ""
        )
        await message.reply(f"👂 Снова записываю этот чат.{tail}")
    else:
        await message.reply(
            "Я и так записываю этот чат — по умолчанию слушаю все чаты, куда меня добавили. "
            "Выключить запись здесь — /unlisten."
        )


@own.message(Command("unlisten"), F.chat.type.in_(GROUP_TYPES))
async def on_unlisten(message: Message, config: Config, app_state: State) -> None:
    """Выключить запись. Внутри топика — только этот топик; «/unlisten чат» — весь чат."""
    if _role(config, message, app_state) != "leader":
        return
    title = message.chat.title or str(message.chat.id)
    app_state.note_chat(message.chat.id, title)
    thread_id, topic_name = topic_of(message)
    if topic_name:
        app_state.note_topic(message.chat.id, thread_id, topic_name)

    if thread_id and not _whole_chat_asked(message):
        name = topic_name or app_state.topic_name(message.chat.id, thread_id) or f"топик {thread_id}"
        if app_state.mute_topic(message.chat.id, thread_id, name):
            await message.reply(
                f"Больше не записываю топик «{name}» — остальные топики этого чата пишу "
                "как обычно. Записанное раньше сохранилось.\n"
                "Вернуть — /listen здесь же. Заглушить чат целиком — «/unlisten чат»."
            )
        else:
            await message.reply("Этот топик я и не записываю.")
        return

    if app_state.mute_chat(message.chat.id, title):
        await message.reply(
            "Больше не записываю этот чат целиком. Записанное раньше сохранилось.\n"
            "Вернуть запись — /listen."
        )
    else:
        await message.reply("Этот чат я и не записываю.")


# Записывать ли новый чат сразу, не дожидаясь решения руководителя. Если на
# уведомление никто не ответит, безопасный исход — «не записываю».
RECORD_NEW_CHATS_BY_DEFAULT = False


async def _ask_about_new_chat(
    bot: Bot, config: Config, app_state: State, chat_id: int, title: str, is_new: bool
) -> None:
    """Бот оказался в чате: руководитель решает, рабочий он или клиентский.
    Уже известный чат остаётся в прежнем состоянии."""
    if is_new and not RECORD_NEW_CHATS_BY_DEFAULT:
        app_state.mute_chat(chat_id, title)
    listening = app_state.is_listening(chat_id)
    log.info("Бот в чате «%s» (id=%s): новый=%s, запись=%s", title, chat_id, is_new, listening)
    if not is_new and listening:
        return  # знакомый рабочий чат — спрашивать не о чем
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Рабочий — записывать", callback_data=f"lst:hear:{chat_id}"),
        InlineKeyboardButton(text="🚫 Клиентский — нет", callback_data=f"lst:mute:{chat_id}"),
    ]])
    await notify_leaders(
        bot, config,
        f"📌 Меня добавили в чат <b>{to_html(title)}</b>.\n\n"
        f"Пока <b>не записываю</b> его и не отвечаю в нём. Если это рабочий чат отдела — "
        f"нажми «Рабочий». Если в чате есть клиент — «Клиентский»: переписку клиентов "
        f"я записывать не должен.\n"
        f"<i>Без ответа останусь в этом чате молчаливым. Передумать можно в любой "
        f"момент: Меню → Управление → Чаты.</i>",
        keyboard,
    )


@own.my_chat_member(F.chat.type.in_(GROUP_TYPES))
async def on_added_to_chat(event, config: Config, app_state: State) -> None:
    """Бота добавили в чат или удалили. В сам чат бот не пишет: объявление увидел бы клиент."""
    status = event.new_chat_member.status
    title = event.chat.title or str(event.chat.id)
    if status in {"left", "kicked"}:
        app_state.mute_chat(event.chat.id, title)
        log.info("Бота убрали из чата «%s» — запись выключена", title)
        return
    if status not in {"member", "administrator"}:
        return
    is_new = app_state.note_chat(event.chat.id, title)
    await _ask_about_new_chat(event.bot, config, app_state, event.chat.id, title, is_new)


@own.message_reaction()
async def on_reaction(event, app_state: State, chat_log: ChatLog) -> None:
    """Реакции тоже пишутся в поток. Telegram присылает их только боту-администратору чата."""
    if event.chat.type not in GROUP_TYPES or not app_state.is_listening(event.chat.id):
        return
    user = event.user
    emojis = " ".join(
        getattr(r, "emoji", None) or getattr(r, "custom_emoji_id", "") or "?"
        for r in (event.new_reaction or [])
    )
    if not emojis:
        return  # реакцию убрали — записывать нечего
    title = event.chat.title or ""
    app_state.note_chat(event.chat.id, title)
    chat_log.record(
        ChatMessage(
            at="", chat_id=event.chat.id, chat_title=title,
            user_id=(user.id if user else 0),
            user_name=(user.full_name if user else "неизвестно"),
            username=(user.username or "" if user else ""),
            message_id=event.message_id,
            text=f"(реакция {emojis} на сообщение #{event.message_id})",
            kind="reaction",
            # Telegram в апдейте реакции топик не сообщает — реакция идёт в общий поток чата.
            thread_id=0,
        )
    )


# --- Управление записью чатов из лички ---

CHATS_PER_VIEW = 12  # больше — сообщение упирается в лимит Telegram
TOPICS_PER_VIEW = 20


def _chats_view(app_state: State, counts: dict[int, int]) -> tuple[str, InlineKeyboardMarkup]:
    chats = app_state.chats
    lines = ["<b>Чаты, куда меня добавили</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for raw_id, meta in list(chats.items())[:CHATS_PER_VIEW]:
        title = meta.get("title") or raw_id
        muted = bool(meta.get("muted"))
        muted_topics = meta.get("muted_topics") or {}
        recorded = counts.get(int(raw_id), 0)
        if muted:
            lines.append(f"🔇 <b>{to_html(title)}</b> — не записываю (записано раньше: {recorded})")
        else:
            tail = f", кроме топиков: {', '.join(muted_topics.values())}" if muted_topics else ""
            lines.append(f"👂 <b>{to_html(title)}</b> — записываю{to_html(tail)} · {recorded} сообщений")
        rows.append([
            InlineKeyboardButton(
                text=("👂 Включить: " if muted else "🔇 Выключить: ") + _cut(str(title), 24),
                callback_data=f"lst:{'hear' if muted else 'mute'}:{raw_id}",
            ),
            InlineKeyboardButton(text="📂 Топики", callback_data=f"lst:t:{raw_id}"),
        ])
    if len(chats) > CHATS_PER_VIEW:
        lines.append(f"\n…и ещё {len(chats) - CHATS_PER_VIEW} — они есть в состоянии, "
                     "но кнопками показываю первые.")
    lines.append(
        "\nКнопка выключает запись всего чата. Чтобы выключить только часть топиков — "
        "«📂 Топики». То же самое можно сделать командой /unlisten внутри чата или топика."
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _threads_of(chat_log: ChatLog, app_state: State, chat_id: int) -> list[dict]:
    """Топики чата из потока и из заглушённых. Имя топика Telegram отдаёт только при
    его создании, поэтому без имени показываем номер и последнюю реплику."""
    seen: dict[int, dict] = {}
    for msg in chat_log.messages(chat_id, limit=400):
        if not msg.thread_id:
            continue
        row = seen.setdefault(msg.thread_id, {"count": 0, "name": "", "last": ""})
        row["count"] += 1
        row["name"] = row["name"] or msg.topic
        row["last"] = f"{msg.user_name}: {msg.text}"
    for raw_tid, name in app_state.muted_topics(chat_id).items():
        row = seen.setdefault(int(raw_tid), {"count": 0, "name": "", "last": ""})
        row["name"] = row["name"] or name
    out = []
    for tid, row in seen.items():
        name = row["name"] or app_state.topic_name(chat_id, tid) or f"топик #{tid}"
        out.append({"id": tid, "name": name, "count": row["count"], "last": row["last"]})
    out.sort(key=lambda r: -r["count"])
    return out[:TOPICS_PER_VIEW]


def _topics_view(
    app_state: State, threads: list[dict], chat_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    title = app_state.chats.get(str(chat_id), {}).get("title") or str(chat_id)
    muted_topics = app_state.muted_topics(chat_id)
    lines = [f"<b>Топики чата «{to_html(title)}»</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    if not threads:
        lines.append(
            "Пока не видел здесь ни одного сообщения в топиках — показать нечего.\n"
            "Как только в топике что-то напишут, он появится здесь. "
            "Можно и не ждать: <code>/unlisten</code> внутри нужного топика."
        )
    for row in threads:
        muted = str(row["id"]) in muted_topics
        mark = "🔇" if muted else "👂"
        tail = f" · {row['count']} сообщений" if row["count"] else ""
        last = f"\n   <i>{to_html(_cut(row['last'], 60))}</i>" if row["last"] else ""
        lines.append(f"{mark} <b>{to_html(row['name'])}</b>{tail}{last}")
        rows.append([InlineKeyboardButton(
            text=("👂 Включить: " if muted else "🔇 Выключить: ") + _cut(row["name"], 24),
            callback_data=f"lst:{'thear' if muted else 'tmute'}:{chat_id}:{row['id']}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К списку чатов", callback_data="lst:back")])
    lines.append("\nВыключенный топик я не записываю и не отвечаю в нём. Остальные топики "
                 "чата пишутся как обычно.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@own.message(Command("chats"), F.chat.type == ChatType.PRIVATE)
async def on_chats(
    message: Message, config: Config, app_state: State, chat_log: ChatLog
) -> None:
    if _role(config, message, app_state) != "leader":
        return
    if not app_state.chats:
        await message.answer(
            "Меня пока не добавили ни в один чат.\n\n"
            "Добавь в рабочий чат — записывать начну сам и сразу напишу тебе об этом. "
            "Дальше выключить запись можно будет здесь кнопками или командой "
            "<code>/unlisten</code> в самом чате.",
            parse_mode="HTML",
        )
        return
    counts = await asyncio.to_thread(chat_log.counts)
    text, keyboard = _chats_view(app_state, counts)
    text += ("\nВыжимка услышанного — /svodka. Поток уезжает в git раз в сутки "
             "(или /publish сразу).")
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@own.callback_query(F.data.startswith("lst:"))
async def on_listen_buttons(
    callback: CallbackQuery, config: Config, app_state: State, chat_log: ChatLog
) -> None:
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1]
    note = ""
    try:
        if action in {"mute", "hear", "t"}:
            chat_id = int(parts[2])
        elif action in {"tmute", "thear"}:
            chat_id, thread_id = int(parts[2]), int(parts[3])
        else:
            chat_id = 0
    except (IndexError, ValueError):
        await callback.answer("Не понял кнопку", show_alert=True)
        return

    if action == "mute":
        app_state.mute_chat(chat_id)
        note = "Выключил запись чата"
    elif action == "hear":
        app_state.unmute_chat(chat_id)
        note = "Снова записываю чат"
    elif action == "tmute":
        name = app_state.topic_name(chat_id, thread_id)
        app_state.mute_topic(chat_id, thread_id, name)
        note = "Выключил запись топика"
    elif action == "thear":
        app_state.unmute_topic(chat_id, thread_id)
        note = "Снова записываю топик"

    if action in {"t", "tmute", "thear"}:
        threads = await asyncio.to_thread(_threads_of, chat_log, app_state, chat_id)
        text, keyboard = _topics_view(app_state, threads, chat_id)
    else:
        counts = await asyncio.to_thread(chat_log.counts)
        text, keyboard = _chats_view(app_state, counts)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        # Telegram отвергает правку, если текст не изменился, — это не ошибка.
        log.debug("Экран управления записью не обновился", exc_info=True)
    await callback.answer(note or "Готово")


@own.message(Command("files"), F.chat.type == ChatType.PRIVATE)
async def on_files(message: Message, config: Config, app_state: State, files: FileLibrary) -> None:
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return
    entries = list(files.entries.values())
    if not entries:
        await message.answer("Библиотека пока пуста. Пришли файл — добавлю.")
        return
    lines = [f"<b>Файлы в библиотеке: {len(entries)}</b>", ""]
    for entry in entries:
        tags = f"\n   теги: {', '.join(entry.tags)}" if entry.tags else "\n   ⚠️ теги не заданы"
        # Сноска «кто и когда добавил» — последняя служебная строка карточки.
        added = next(
            (ln.strip() for ln in reversed((entry.description or "").splitlines())
             if ln.strip().startswith(("Добавил:", "Прислал:", "Предложил:"))), "",
        )
        lines.append(
            f"• <b>{to_html(entry.title)}</b>{to_html(tags)}"
            + (f"\n   <i>{to_html(added)}</i>" if added else "")
        )
    keyboard = None
    if role == "leader":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✏️ {entry.title[:28]}", callback_data=f"fmeta:{entry.id}")]
            for entry in entries[:12]
        ])
        lines.append("\nКнопка — поправить название, теги и описание.")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


@own.message(Command("svodka"), F.chat.type == ChatType.PRIVATE)
async def on_svodka(
    message: Message, config: Config, app_state: State,
    chat_log: ChatLog, kb_editor: KbEditor,
) -> None:
    """Выжимка услышанного в рабочих чатах: только показывает, в базу ничего не пишет."""
    if _role(config, message, app_state) != "leader":
        return
    chats = {k: v for k, v in app_state.chats.items() if not v.get("muted")}
    if not chats:
        await message.answer("Пока нечего слушать — сводку собирать не из чего. /chats")
        return

    await message.answer("Читаю записанное, собираю сводку…")
    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        for raw_id, meta in list(chats.items())[:SVODKA_CHAT_LIMIT]:
            chat_id = int(raw_id)
            stream = await asyncio.to_thread(chat_log.as_text, chat_id, SVODKA_MSG_LIMIT)
            if len(stream) > SVODKA_MAX_CHARS:
                # Берём конец потока: сводка нужна про свежее.
                stream = stream[-SVODKA_MAX_CHARS:].split("\n", 1)[-1]
            title = meta.get("title", raw_id)
            if not stream.strip():
                await message.answer(f"<b>{to_html(title)}</b>: записей пока нет.", parse_mode="HTML")
                continue
            digest = await kb_editor.summarize_chat(title, stream, model=app_state.model)
            await _send(message, f"<b>Сводка: {to_html(title)}</b>\n\n{digest}", already_html_head=True)
    except Exception:
        log.exception("Не смог собрать сводку")
        await message.answer("Не смог собрать сводку — попробуй ещё раз позже.")
    finally:
        typing.cancel()


@own.message(F.chat.type.in_(GROUP_TYPES))
async def on_group_message(
    message: Message,
    config: Config,
    app_state: State,
    agent: Agent,
    chat_log: ChatLog,
    answer_cache: AnswerCache,
    usage_log: UsageLog,
    auto_writer: AutoWriter,
    links: LinkBook,
    publisher: Publisher,
    kb: KnowledgeBase,
    transcript: Transcript,
) -> None:
    """Рабочий чат: пишем поток и отвечаем, когда к боту обратились. Регистрируется
    последним среди групповых; в заглушённых чатах не делает ничего."""
    if message.from_user is None or not app_state.is_listening(message.chat.id):
        return
    # Чат без события о добавлении регистрируем при первом сообщении.
    if app_state.note_chat(message.chat.id, message.chat.title or ""):
        await _ask_about_new_chat(
            message.bot, config, app_state, message.chat.id,
            message.chat.title or str(message.chat.id), True,
        )
        if not app_state.is_listening(message.chat.id):
            return

    # Поток помечаем топиком — иначе сводка смешает топики.
    thread_id, topic_name = topic_of(message)
    if topic_name:
        app_state.note_topic(message.chat.id, thread_id, topic_name)
    elif thread_id:
        topic_name = app_state.topic_name(message.chat.id, thread_id)

    # Заглушённый топик: не пишем поток и не отвечаем.
    if thread_id and not app_state.is_listening(message.chat.id, thread_id):
        return

    if message.forum_topic_created is not None:
        return  # служебное сообщение о создании топика: имя забрали, писать нечего

    kind, text = describe_kind(message)
    chat_log.record(
        ChatMessage(
            at="",  # ставится при записи
            chat_id=message.chat.id,
            chat_title=message.chat.title or "",
            user_id=message.from_user.id,
            user_name=message.from_user.full_name,
            username=message.from_user.username or "",
            message_id=message.message_id,
            text=text,
            kind=kind,
            thread_id=thread_id,
            topic=topic_name,
        )
    )
    app_state.note_person(message.from_user.username, message.from_user.full_name)

    question = await _question_for_bot(message)
    if not question:
        return  # к нам не обращались — просто слушаем

    # В записываемом чате отвечаем всем, кто обратился: доступ решается на уровне
    # чата, а не человека (заглушённый чат сюда не доходит вовсе).
    known = resolve_role(config, app_state, message.from_user.id, message.from_user.username)
    log.info(
        "Обращение в чате «%s» от %s (@%s, %s): %s",
        message.chat.title, message.from_user.id, message.from_user.username or "нет",
        known or "не в whitelist", question[:150],
    )

    # Правка базы прямо из чата: писать может любой участник; страховка — автор у каждой
    # правки, класс риска и невозможность удаления.
    intent, intent_usage = await agent.classify_intent(question)
    usage_log.record(message.from_user.id, app_state.model, intent_usage, 0.0)
    if intent in (qtype.EDIT, qtype.LINK, qtype.DENY):
        await _chat_edit(
            message, app_state, auto_writer, links, publisher, kb, answer_cache,
            question, intent,
        )
        return
    # В чате отвечаем без истории: говорят несколько человек одновременно.
    # Вложение до текстовой модели не доходит — если весь смысл в картинке, честно отказываемся.
    unseen = _unseen_attachment(message, question)
    note = ""
    if unseen is not None:
        refuse, unseen_text = unseen
        if refuse:
            await message.reply(unseen_text)
            return
        note = unseen_text + "\n\n"  # вопрос самостоятельный — отвечаем с оговоркой

    # Контекст топика собирается до кэша — он часть вопроса.
    context = _chat_context(chat_log, message, thread_id)

    # У чата свой раздел кэша (формат короче), а вопрос с контекстом из кэша не отдаём:
    # общий ключ склеил бы разные разговоры.
    cached = (
        answer_cache.get(question, app_state.model, scope=CHAT_SCOPE)
        if not context else None
    )
    if cached is not None:
        await _send_answer(
            message, cached.answer, question, app_state.model,
            kind=cached.kind, units=cached.units, in_group=True,
        )
        return

    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        result = await agent.answer(
            Dialog(), question, model=app_state.model, in_group=True, context=context,
        )
    except Exception:
        log.exception("Не смог ответить в чате %s", message.chat.id)
        return  # в рабочем чате молчим об ошибке, чтобы не мусорить
    finally:
        typing.cancel()

    usage_log.record(message.from_user.id, app_state.model, result.usage, 0.0)
    transcript.record(
        message.from_user.id, message.from_user.full_name, app_state.model,
        question, result.text, 0.0, kind=result.kind, units=result.units,
    )
    log.info(
        "Ответ в чате %s | тип %s | единицы %s | %s",
        message.chat.id, qtype.LABELS.get(result.kind, result.kind),
        ", ".join(result.units) or "нет", result.usage,
    )
    # Ответ с оговоркой про невидимое вложение не кэшируем.
    if not result.files and not context and not note:
        answer_cache.put(
            question, result.text, app_state.model,
            units=result.units, kind=result.kind, scope=CHAT_SCOPE,
        )
    await _send_answer(
        message, note + result.text, question, app_state.model,
        kind=result.kind, units=result.units, in_group=True,
    )
    await _send_files(message, result.files)


async def _private_intent(
    message: Message, config: Config, app_state: State, agent: Agent,
    kb: KnowledgeBase, kb_editor: KbEditor, links: LinkBook, publisher: Publisher,
    answer_cache: AnswerCache, auto_writer: AutoWriter,
    text: str, intent: str, is_admin: bool,
) -> None:
    """Личка: человек не спрашивает, а сообщает — ссылку, факт или возражение."""
    author = message.from_user.full_name or f"@{message.from_user.username or '?'}"

    if intent == qtype.DENY:
        await _mark_unverified(message, kb, publisher, answer_cache, text, author)
        return

    if intent == qtype.LINK:
        url = _first_url(text)
        if not url:
            await message.answer("Ссылку не вижу — пришли её целиком, вместе с http…")
            return
        await _save_link(message, links, publisher, _link_title(text, url) + " " + url, author)
        return

    # intent == EDIT
    await _run_update(message.bot, message.chat.id, message.from_user.id,
                      app_state, kb_editor, text,
                      author=message.from_user.full_name, leader=is_admin)


async def _mark_unverified(
    message: Message, kb: KnowledgeBase, publisher: Publisher,
    answer_cache: AnswerCache, text: str, author: str,
) -> None:
    """Возражение «<id> неверно»: помечаем единицу непроверенной, не откатывая текст
    (см. `kb_write.mark_unverified`)."""
    found = re.search(r"kb-[0-9]{3,4}", text, re.IGNORECASE)
    if not found:
        await message.reply(
            "Понял, что записанное неверно. Назови единицу — <code>kb-101</code>, "
            "например, — и я помечу её как непроверенную.",
            parse_mode="HTML",
        )
        return
    kb_id = found.group(0).lower()
    async with KB_WRITE_LOCK:
        result = await asyncio.to_thread(
            kb_write.mark_unverified, kb, publisher, kb_id,
            f"База (бот): {kb_id} помечена непроверенной по возражению — {author}",
        )
        if result.wrote:
            kb.reload()
            answer_cache.sync(kb.unit_hashes())
    if result.wrote:
        await message.reply(
            f"Пометил <b>{kb_id}</b> как непроверенную — отвечать по ней буду "
            f"с оговоркой. Текст не тронул, попадёт в недельный отчёт.",
            parse_mode="HTML",
        )
    else:
        await message.reply(result.message)
    log.info("Возражение от %s по %s: %s", author, kb_id, result.message)


async def _chat_edit(
    message: Message, app_state: State, auto_writer: AutoWriter, links: LinkBook,
    publisher: Publisher, kb: KnowledgeBase, answer_cache: AnswerCache,
    text: str, intent: str,
) -> None:
    """Запись в базу из рабочего чата: показываем формулировку и ждём подтверждения автора."""
    author = message.from_user.full_name or f"@{message.from_user.username or '?'}"
    origin = f"чат {message.chat.title or message.chat.id}, {author}"

    if intent == qtype.DENY:
        await _mark_unverified(message, kb, publisher, answer_cache, text, author)
        return

    if intent == qtype.LINK:
        url = _first_url(text)
        if not url:
            await message.reply("Ссылку не вижу — пришли её текстом, сохраню.")
            return
        title = _link_title(text, url)
        ok, note = links.add(title, url, _link_tags(text), author)
        if ok:
            await asyncio.to_thread(
                publisher.commit_paths, [links_module.REL_PATH],
                f"Ссылки (бот): {title} — из чата, {author}",
            )
        await message.reply(note if not ok else f"Сохранил ссылку: {title}")
        log.info("Ссылка из чата (%s): %s — %s", author, url, note)
        return

    # intent == EDIT
    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    prov = {
        "who": author,
        "who_id": message.from_user.id,
        "msg": f"{message.chat.title or message.chat.id}#{message.message_id}",
        "said": message.date.isoformat() if message.date else "",
    }
    try:
        draft = await auto_writer.draft(
            text, origin, model=app_state.model, prov=prov, confirmed=True,
        )
    except Exception:
        log.exception("Правка из чата не удалась: %s", text[:120])
        await message.reply("Не смог разобрать, куда это записать. Переформулируй?")
        return
    finally:
        typing.cancel()

    if isinstance(draft, dict):
        # dict — записывать нечего (тема не подобралась, правка отклонена).
        await message.reply(_chat_edit_reply(draft), parse_mode="HTML")
        log.info("Правка из чата (%s) не подготовлена: %s, %s", author,
                 draft.get("class"), draft.get("reason", "")[:100])
        return

    draft_id = _next_pending_id()
    PENDING_CHAT_DRAFTS[draft_id] = (message.from_user.id, draft, time.time())
    _trim_dict(PENDING_CHAT_DRAFTS, limit=100)
    await message.reply(
        _draft_preview(draft, author),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Верно, записать", callback_data=f"cw:yes:{draft_id}"),
            InlineKeyboardButton(text="❌ Не надо", callback_data=f"cw:no:{draft_id}"),
        ]]),
    )
    log.info("Черновик из чата #%s (%s): %s, класс %s", draft_id, author,
             draft.kb_id, draft.level)


def _draft_preview(draft, author: str) -> str:
    """Что человек подтверждает: куда и какими словами ляжет его факт."""
    head = (
        f"📝 <b>Заведу новую тему</b> «{to_html(draft.title)}» и запишу так:"
        if draft.kind == "new"
        else f"📝 <b>Запишу так</b> — в тему «{to_html(draft.title)}»:"
    )
    body = _clip_text("\n".join(draft.added), 1200) or draft.fact
    lines = [head, "", to_html(body)]
    change = getattr(draft.proposal, "state_change", None)
    if change and change[1]:
        lines += ["", f"🔄 Было: {to_html(change[1])}\nСтарое значение не пропадёт — уйдёт в «Было раньше»."]
    # При замене состояния прежнее значение не пропадает — «removed» не дублируем.
    gone = [] if change else list(getattr(draft, "removed", []) or [])
    if gone:
        lines += [
            "",
            "⚠️ <b>Уберу или заменю прежний текст:</b>",
            to_html(_clip_text("\n".join(gone), 800)),
        ]
    lines += [
        "",
        f"<i>Под записью будет стоять: внёс {to_html(author)}, сегодняшняя дата. "
        f"Верно? Подтвердить может только {to_html(author)}; без ответа за сутки "
        f"ничего не запишу. Не так — нажми «Не надо» и напиши точнее.</i>",
    ]
    return "\n".join(lines)


@own.callback_query(F.data.startswith("cw:"))
async def on_chat_draft_decision(callback: CallbackQuery, auto_writer: AutoWriter) -> None:
    """Автор подтверждает или отклоняет формулировку. Роль не проверяется намеренно
    (граница доступа в рабочем чате — сам чат); проверяется, что кнопку жмёт автор."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    raw_id = parts[2] if len(parts) > 2 else ""
    row = PENDING_CHAT_DRAFTS.get(int(raw_id)) if raw_id.isdigit() else None
    if row is None:
        await callback.answer("Черновик устарел — напиши факт ещё раз.", show_alert=True)
        await _clear_markup(callback)
        return
    author_id, draft, created = row
    if callback.from_user.id != author_id:
        await callback.answer("Подтвердить может только автор сообщения.", show_alert=True)
        return
    PENDING_CHAT_DRAFTS.pop(int(raw_id), None)
    await _clear_markup(callback)
    if time.time() - created > CHAT_DRAFT_TTL:
        await callback.answer("Прошло больше суток — напиши факт ещё раз.", show_alert=True)
        return
    if action != "yes":
        await callback.answer("Не записываю")
        await callback.message.reply("Ок, не записал. Если нужно — напиши точнее, покажу снова.")
        return

    await callback.answer("Записываю…")
    try:
        result = await auto_writer.commit(draft)
    except Exception:
        log.exception("Запись черновика из чата не удалась: %s", draft.fact[:120])
        await callback.message.reply("Не смог записать — что-то сломалось. Попробуй ещё раз позже.")
        return
    await callback.message.reply(_chat_edit_reply(result), parse_mode="HTML")
    log.info("Черновик из чата записан (%s): %s, %s", callback.from_user.id,
             result.get("class"), result.get("kb_id", ""))


def _chat_edit_reply(result: dict) -> str:
    """Ответ в чат по итогу правки: не говорить «записал», когда не записал."""
    level = result.get("class")
    kb_id = result.get("kb_id", "")
    summary = to_html(result.get("summary", ""))
    if level in ("green", "yellow") or (level == "red" and result.get("confirmed")):
        signed = " Подпись и дата — под записью." if result.get("confirmed") else ""
        return f"✅ Записал в <b>{kb_id}</b>: {summary}.{signed}"
    if level == "red":
        return (
            f"✅ Записал в <b>{kb_id}</b>: {summary}\n"
            f"⚠️ Пометил как непроверенное — {to_html(result.get('reason', ''))}. "
            f"Отвечать по нему буду с оговоркой."
        )
    if level == "blocked":
        return (
            f"⛔ Не стал записывать: {to_html(result.get('reason', ''))}.\n"
            f"Это защита от порчи текста — сформулируй как дополнение, не как замену."
        )
    if level == "skip":
        return (
            f"Не стал заводить под это отдельную тему: {to_html(result.get('reason', ''))}.\n"
            f"Если это правда нужно в базе — сформулируй как правило, "
            f"и я запишу."
        )
    if level == "limit":
        return "Сегодня уже записал максимум правок. Повтори завтра или скажи руководителю."
    if level == "off":
        return "Автономная запись сейчас выключена — правку не сохранил."
    # human: маршрутизация не сошлась
    return (
        f"🤔 Понял как факт для базы, но не смог решить, куда его положить: "
        f"{to_html(result.get('reason', ''))}.\n"
        f"Уточни, к какой теме относится, — или руководитель увидит это в недельном отчёте."
    )


_URL_RE = re.compile(r"https?://\S+")


def _first_url(text: str) -> str:
    found = _URL_RE.search(text or "")
    return found.group(0).rstrip(".,;)»") if found else ""


def _link_title(text: str, url: str) -> str:
    """Название ссылки — текст вокруг неё, без самой ссылки и без обращения к боту."""
    cleaned = (text or "").replace(url, " ")
    cleaned = re.sub(r"@\w+", " ", cleaned)
    cleaned = re.sub(
        r"\b(запомни|сохрани|добавь|пусть будет|ссылк\w*|это|вот)\b", " ", cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = " ".join(cleaned.split()).strip(" -—:·")
    return cleaned[:90] or "Ссылка из рабочего чата"


def _link_tags(text: str) -> list[str]:
    """Теги для поиска словами — значимые слова из подписи к ссылке."""
    words = re.findall(r"[а-яёa-z0-9]{4,}", (text or "").lower())
    skip = {"запомни", "сохрани", "добавь", "ссылка", "ссылку", "http", "https"}
    seen: list[str] = []
    for word in words:
        if word not in skip and word not in seen:
            seen.append(word)
    return seen[:6]


# Зачины вопроса-обрывка («а если», «и как это»): на них окно контекста расширяется.
_FRAGMENT = re.compile(
    r"^\s*(а\b|и\b|но\b|тогда\b|значит\b|получается\b|это\b|там\b|так\b|ну\b|ок\b|"
    r"почему\b|зачем\b|как\b|когда\b|сколько\b|что\b)",
    re.IGNORECASE,
)


def _needs_deep_context(question: str) -> bool:
    """Мало своего смысла в вопросе (короткий или с зачина-связки) — окно контекста шире."""
    words = dedupe.tokens(question)
    return len(words) < 5 or bool(_FRAGMENT.match(question or ""))


def _chat_context(chat_log: ChatLog, message: Message, thread_id: int) -> str:
    """Хвост того же топика плюс сообщение, на которое отвечают (ответов бота в потоке
    нет, Telegram отдаёт реплай в апдейте); всё обёрнуто как данные, не команды."""
    lines: list[str] = []
    reply = message.reply_to_message
    if reply is not None:
        who = "ты (бот)" if getattr(reply.from_user, "is_bot", False) else (
            f"@{reply.from_user.username}" if reply.from_user and reply.from_user.username
            else (reply.from_user.full_name if reply.from_user else "кто-то")
        )
        body = (reply.text or reply.caption or "").strip()
        if body:
            lines.append(f"[отвечают на это сообщение] {who}: {body[:1500]}")

    # Окно тем шире, чем меньше в вопросе своего смысла.
    question = (message.text or message.caption or "").strip()
    deep = _needs_deep_context(question)
    limit = CHAT_CONTEXT_DEEP if deep else CHAT_CONTEXT_MESSAGES
    history = chat_log.context(
        message.chat.id, thread_id,
        limit=limit, skip_message_id=message.message_id,
    )
    lines.extend(m.as_line() for m in history)
    if not lines:
        return ""
    log.info(
        "Контекст топика %s/%s: %d сообщений", message.chat.id, thread_id, len(lines)
    )
    return as_data("\n".join(lines), "ПОСЛЕДНИЕ СООБЩЕНИЯ В ЭТОМ ТОПИКЕ")


# Подпись, которая показывает на вложение, а не спрашивает по базе.
_POINTS_AT_IMAGE = re.compile(
    r"^\s*(что|чего|как|почему|зачем|куда|где|кто|это|тут|здесь|а\b|и\b|норм|ок)\b"
    r".{0,40}$|^\s*(посмотри|глянь|проверь|подскажи|помоги|объясни)\b.{0,30}$",
    re.IGNORECASE,
)

# Ниже этой длины подпись почти наверняка не самостоятельный вопрос.
_MIN_STANDALONE_QUESTION = 25


def _unseen_attachment(message: Message, question: str) -> tuple[bool, str] | None:
    """Вложение, которого модель не видит: None — вложения нет; (True, отказ) — подпись
    показывает на картинку; (False, оговорка) — подпись сама по себе вопрос."""
    what = None
    if message.photo:
        what = "картинку"
    elif message.video or message.video_note:
        what = "видео"
    elif message.voice or message.audio:
        what = "голосовое"
    elif message.document:
        name = getattr(message.document, "file_name", "") or "файл"
        what = f"файл «{name}»"
    if what is None:
        return None  # стикеры и обычный текст

    body = (question or "").strip()
    standalone = len(body) >= _MIN_STANDALONE_QUESTION and not _POINTS_AT_IMAGE.match(body)
    if standalone:
        return False, f"⚠️ {what.capitalize()} я не вижу, отвечаю только по тексту вопроса."
    if message.document:
        return True, (
            f"{what.capitalize()} в чате я не разбираю — пришли его мне в личку, "
            f"там я достану текст и отвечу."
        )
    return True, (
        f"{what.capitalize()} я не вижу — я текстовый. Опиши словами, что там "
        f"(цифры, названия, что именно смущает), и я отвечу по базе."
    )


async def _question_for_bot(message: Message) -> str | None:
    """Обращались ли к боту: упоминание @ника или ответ на его сообщение
    (Group Privacy выключен, бот видит весь поток)."""
    me = await message.bot.me()
    text = message.text or message.caption or ""
    reply = message.reply_to_message
    if reply is not None and reply.from_user and reply.from_user.id == me.id:
        return text.strip() or None
    mention = f"@{me.username}".lower()
    if me.username and mention in text.lower():
        cleaned = re.sub(re.escape(mention), " ", text, flags=re.IGNORECASE).strip()
        return cleaned or None
    return None


@own.message(F.text)
async def on_question(
    message: Message,
    config: Config,
    agent: Agent,
    app_state: State,
    usage_log: UsageLog,
    transcript: Transcript,
    files: FileLibrary,
    suggestion_log: SuggestionLog,
    kb: KnowledgeBase,
    kb_editor: KbEditor,
    links: LinkBook,
    publisher: Publisher,
    answer_cache: AnswerCache,
    dialog_store: DialogStore,
    auto_writer: AutoWriter,
) -> None:
    if message.from_user is None:
        return
    # Личка; группы обрабатывает on_group_message.
    if message.chat.type != ChatType.PRIVATE:
        return
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return

    user = message.from_user
    text = (message.text or "").strip()
    # Кнопку старого меню понимаем как новую.
    text = LEGACY_LABELS.get(text, text)
    is_admin = role == "leader"
    app_state.note_person(user.username, user.full_name)
    app_state.note_manager_id(user.id, user.username, user.full_name)
    app_state.note_user(user.id, user.username, user.full_name)
    if is_admin:
        app_state.name_leader(user.id, user.full_name)

    # Неизвестная команда в режиме ввода — человек передумал (известные команды
    # перехватывают свои обработчики).
    if text.startswith("/") and _forget_pending_text(user.id):
        await message.answer("Отменил режим ввода — вернулся в обычный.", reply_markup=_menu_for(role))
        return

    # Подпись кнопки меню приходит обычным сообщением: в режиме ожидания текста это
    # отмена, а не описание правки или файла.
    if text in MENU_LABELS:
        for waiting in (PENDING_UPDATE, PENDING_PROPOSAL, PENDING_SUGGESTION):
            waiting.discard(user.id)
        if user.id in PENDING_UPLOADS:
            PENDING_UPLOADS.pop(user.id).staged.unlink(missing_ok=True)
            PENDING_FORWARDS.pop(user.id, None)
            await message.answer("Отменил загрузку файла — вернулся в меню.")

    # Незавершённая загрузка — этот текст и есть описание файла.
    if user.id in PENDING_UPLOADS:
        await _finish_upload(message, files, config, app_state)
        return

    # Правка карточки файла — этот текст и есть новая карточка.
    if user.id in PENDING_FILE_META:
        await _finish_file_meta(message, files, publisher)
        return

    # Голая ссылка — не вопрос в модель: спрашиваем, что с ней сделать.
    url = _first_url(text)
    if url and len(text) < 300 and "?" not in text:
        PENDING_LINK[user.id] = text
        await message.answer(
            "Это ссылка — что с ней сделать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔗 В полезные", callback_data="linkq:save"),
                InlineKeyboardButton(text="❓ Это вопрос", callback_data="linkq:ask"),
            ]]),
        )
        return

    # Режим записи в базу — этот текст и есть описание правки; PENDING_PROPOSAL —
    # старая кнопка «Предложить в базу», ведёт туда же.
    if user.id in PENDING_UPDATE or user.id in PENDING_PROPOSAL:
        PENDING_UPDATE.discard(user.id)
        PENDING_PROPOSAL.discard(user.id)
        if _is_cancel(text):
            await message.answer("Ок, отменил.", reply_markup=_menu_for(role))
            return
        await _run_update(
            message.bot, message.chat.id, user.id, app_state, kb_editor, text,
            author=user.full_name, leader=is_admin,
        )
        return

    # Режим «Предложение» — этот текст и есть предложение.
    if user.id in PENDING_SUGGESTION:
        PENDING_SUGGESTION.discard(user.id)
        if _is_cancel(text):
            await message.answer("Ок, отменил.", reply_markup=_menu_for(role))
            return
        suggestion_log.record(user.id, user.full_name, text)
        await message.answer("Записал предложение — спасибо! Обсудим.", reply_markup=_menu_for(role))
        return

    # Кнопки постоянного меню.
    if text == BTN_HELP:
        await message.answer(
            GREETING + (ADMIN_HELP if is_admin else MANAGER_HELP),
            parse_mode="HTML", reply_markup=_menu_for(role),
        )
        return
    if text == BTN_FILES:
        # Кнопка — витрина (что есть: ссылки и файлы), текст — выдача конкретного.
        await message.answer(
            _showcase(files, links), parse_mode="HTML",
            reply_markup=_menu_for(role), disable_web_page_preview=True,
        )
        return
    if text == BTN_SUGGEST:
        PENDING_SUGGESTION.add(user.id)
        await message.answer(
            "Напиши идею или пожелание одним сообщением — передам руководителю.\n"
            "Это про работу бота и процессы. Если хочешь дополнить саму базу знаний — "
            "жми «✏️ Предложить в базу».",
            reply_markup=_menu_for(role),
        )
        return
    # «Предложить в базу» и «Обновить базу» — одно действие.
    if text in (BTN_PROPOSE, BTN_UPDATE):
        PENDING_UPDATE.add(user.id)
        await message.answer(
            "Напиши одним сообщением, что внести или поправить, — например «минимальный "
            "срок договора теперь три месяца». Найду тему, покажу, как именно запишу, и спрошу "
            "«верно?». Передумал — «отмена».",
            reply_markup=_menu_for(role),
        )
        return
    if text == BTN_USERS and is_admin:
        await on_users(message, config, app_state)
        return
    # Кнопки зовут те же обработчики, что и команды.
    if text == BTN_PENDING and is_admin:
        await on_pending_facts(message, config, app_state)
        return
    if text == BTN_HYPOTHESES and is_admin:
        await on_hypotheses(message, config, app_state, kb)
        return
    if text == BTN_CHANGES:
        await _send_period_report(message, app_state, kb, kb_editor, days=7)
        return


    # Вопрос про период словами («что я пропустил за неделю») — сводка, а не ответ
    # по единицам: тот подбирает единицы по смыслу и периода не понимает.
    period_days = period.asked_period(text)
    if period_days is not None:
        log.info("Вопрос про период от %s: показываю сводку за %d дн.", user.id, period_days)
        await _send_period_report(message, app_state, kb, kb_editor, days=period_days)
        return
    # «запомни ссылку …» — сохраняем от любого, кто в доступе; автор виден в коммите.
    m = _LINK_PREFIX.match(text)
    if m:
        await _save_link(message, links, publisher, text[m.end():].strip(), user.full_name)
        return

    # Текстовый зачин правки без кнопки: «обнови базу: …».
    m = _UPDATE_PREFIX.match(text)
    if m and text[m.end():].strip():
        payload = text[m.end():].strip()
        await _run_update(
            message.bot, message.chat.id, user.id, app_state, kb_editor, payload,
            author=user.full_name, leader=is_admin,
        )
        return

    # Зачины выше — быстрый путь без модели; остальное разбирает классификатор намерения.
    intent, intent_usage = await agent.classify_intent(text)
    usage_log.record(user.id, app_state.model, intent_usage, 0.0)
    if intent != qtype.ASK:
        await _private_intent(
            message, config, app_state, agent, kb, kb_editor, links, publisher,
            answer_cache, auto_writer, text, intent, is_admin,
        )
        return

    question = text
    if not question:
        return

    log.info("Вопрос от %s (%s): %s", user.id, user.full_name, question[:200])
    dialog = dialog_store.get(user.id)
    # Кэшируем только первый вопрос разговора: ответ на уточнение зависит от истории.
    fresh_dialog = not dialog.messages

    if fresh_dialog:
        cached = answer_cache.get(question, app_state.model)
        if cached is not None:
            # Ответ из кэша тоже кладём в историю вместе с типом запроса, иначе
            # уточнение следом останется без контекста.
            dialog.add({"role": "user", "content": question})
            dialog.add({"role": "assistant", "content": cached.answer})
            dialog.kind = cached.kind or dialog.kind
            dialog_store.touch(user.id)
            transcript.record(
                user.id, user.full_name, f"{app_state.model} (кэш)", question,
                cached.answer, 0.0, kind=cached.kind, units=cached.units,
            )
            await _send_answer(
                message, cached.answer, question, app_state.model,
                kind=cached.kind, units=cached.units,
            )
            return

    started = time.monotonic()
    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    # Лок на пользователя: второй быстрый вопрос дождётся первого, а не смешает историю.
    try:
        async with _user_lock(user.id):
            result = await agent.answer(dialog, question, model=app_state.model)
    except Exception:
        log.exception("Не смог ответить на вопрос от %s", user.id)
        await message.answer(
            "Что-то сломалось на моей стороне. Попробуй ещё раз через минуту — "
            "если повторится, скажи руководителю."
        )
        return
    finally:
        typing.cancel()

    seconds = time.monotonic() - started
    dialog_store.touch(user.id)
    usage_log.record(user.id, app_state.model, result.usage, seconds)
    transcript.record(
        user.id, user.full_name, app_state.model, question, result.text, seconds,
        kind=result.kind, units=result.units,
    )
    # Единицы пользователю не показываем — лог единственное место, где видно, на чём построен ответ.
    log.info(
        "Ответ для %s за %.1f сек | тип %s | единицы %s | %s",
        user.id, seconds, qtype.LABELS.get(result.kind, result.kind),
        ", ".join(result.units) or "нет", result.usage,
    )
    # Ответ с файлами не кэшируем: второму спросившему сам файл не ушёл бы.
    if fresh_dialog and not result.files:
        answer_cache.put(
            question, result.text, app_state.model,
            units=result.units, kind=result.kind,
        )
    await _send_answer(
        message, result.text, question, app_state.model,
        kind=result.kind, units=result.units,
    )
    await _send_files(message, result.files)


# Регистрируется последним: ловит всё, что не текст и не команда, иначе бот молчит и кажется сломанным.
@own.message()
async def on_non_text(message: Message, config: Config, app_state: State) -> None:
    if message.from_user is None or message.chat.type != ChatType.PRIVATE:
        return
    if not _role(config, message, app_state):
        await _deny(message, config)
        return
    await message.answer(
        "Пока понимаю только текст. Напиши вопрос словами — "
        f"например «{kbconfig.CFG.example('question')}» или «у клиента просели показатели»."
    )


async def _finish_upload(
    message: Message, files: FileLibrary, config: Config, app_state: State
) -> None:
    """Второй шаг загрузки: пришло описание — дописываем метаданные и включаем файл."""
    user_id = message.from_user.id
    pending = PENDING_UPLOADS.pop(user_id, None)
    PENDING_FORWARDS.pop(user_id, None)
    if pending is None:
        return

    text = (message.text or "").strip()
    if _is_cancel(text):
        pending.staged.unlink(missing_ok=True)
        await message.answer("Отменил — файл не сохранил.")
        return

    if pending.simple:
        # Описание одной фразой: первая строка — название, теги пустые.
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        title, tags, description = (first[:90] or pending.original_name), [], text
    else:
        title, tags, description = uploads.parse_meta(text)
    added_by = pending.added_by or message.from_user.full_name
    try:
        file_id, filename = await asyncio.to_thread(
            uploads.finalize, files.dir, pending, title, tags, description, added_by
        )
        files.reload()
    except Exception:
        log.exception("Не смог сохранить загруженный файл от %s", user_id)
        pending.staged.unlink(missing_ok=True)
        await message.answer("Не смог сохранить файл. Попробуй загрузить заново.")
        return

    await message.answer(
        f"Готово, файл в библиотеке:\n"
        f"<b>{to_html(title)}</b>\n"
        f"теги: {to_html(', '.join(tags)) if tags else '—'}\n"
        f"<i>{to_html(uploads.footnote(added_by))}</i>\n\n"
        f"Его уже можно запросить у меня словами. В git уйдёт при ближайшей "
        f"суточной публикации.",
        parse_mode="HTML",
    )


@own.callback_query(F.data.startswith("linkq:"))
async def on_link_question(
    callback: CallbackQuery, config: Config, app_state: State,
    links: LinkBook, publisher: Publisher, agent: Agent, usage_log: UsageLog,
) -> None:
    """Человек прислал ссылку: сохранить её или это всё-таки вопрос."""
    role = resolve_role(config, app_state, callback.from_user.id, callback.from_user.username)
    if not role:
        await callback.answer()
        return
    text = PENDING_LINK.pop(callback.from_user.id, "")
    await _clear_markup(callback)
    if not text:
        await callback.answer("Уже неактуально")
        return
    if callback.data.endswith(":ask"):
        await callback.answer("Понял, отвечаю")
        await callback.message.answer(
            "Хорошо — тогда задай вопрос словами, без ссылки: так я точно пойму, "
            "что именно нужно из базы."
        )
        return
    # Ссылку сохраняем от любого, кто в доступе; автор виден в коммите.
    await callback.answer("Сохраняю")
    payload = _LINK_PREFIX.sub("", text)
    await _save_link(callback.message, links, publisher, payload, callback.from_user.full_name)


@own.callback_query(F.data.startswith("fmeta:"))
async def on_file_meta(
    callback: CallbackQuery, config: Config, app_state: State, files: FileLibrary
) -> None:
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    file_id = callback.data.split(":", 1)[1]
    entry = files.entries.get(file_id)
    if entry is None:
        await callback.answer("Файл не найден — возможно, его уже удалили", show_alert=True)
        return
    PENDING_FILE_META[callback.from_user.id] = file_id
    await callback.answer()
    await callback.message.answer(
        f"Правлю карточку файла <b>{to_html(entry.title)}</b>.\n\n"
        f"Пришли одним сообщением:\n"
        f"• строка 1 — <b>название</b>\n"
        f"• строка 2 — <b>теги</b> через запятую\n"
        f"• дальше — описание\n\n"
        f"Сейчас:\n<code>{to_html(entry.title)}\n{to_html(', '.join(entry.tags) or '—')}\n"
        f"{to_html(entry.description[:200])}</code>\n\nИли «отмена».",
        parse_mode="HTML",
    )


async def _finish_file_meta(
    message: Message, files: FileLibrary, publisher: Publisher
) -> None:
    """Переписываем карточку файла по присланному тексту и коммитим."""
    file_id = PENDING_FILE_META.pop(message.from_user.id, None)
    if file_id is None:
        return
    text = (message.text or "").strip()
    if _is_cancel(text):
        await message.answer("Отменил, карточку не трогал.")
        return
    entry = files.entries.get(file_id)
    if entry is None:
        await message.answer("Файл куда-то делся — открой /files заново.")
        return
    title, tags, description = uploads.parse_meta(text)
    try:
        await asyncio.to_thread(
            uploads.rewrite_meta, files.dir, entry, title or entry.title, tags, description
        )
        files.reload()
    except Exception:
        log.exception("Не смог переписать карточку файла %s", file_id)
        await message.answer("Не смог переписать карточку. Посмотри логи.")
        return
    git_ok, git_msg = await asyncio.to_thread(
        publisher.publish, f"карточка файла {file_id}"
    )
    await message.answer(
        f"Готово:\n<b>{to_html(title or entry.title)}</b>\n"
        f"теги: {', '.join(tags) if tags else '—'}\n\n"
        f"Менеджеры найдут его по этим словам."
        + ("" if git_ok else f"\n\n⚠️ В git не ушло: {to_html(git_msg)}"),
        parse_mode="HTML",
    )


# Согласование ссылок и файлов от менеджера отменено: обработчики `lprop:` и `fprop:`
# оставлены ради кнопок под старыми сообщениями.
@own.callback_query(F.data.startswith("lprop:"))
async def on_link_proposal(
    callback: CallbackQuery, config: Config, app_state: State,
    links: LinkBook, publisher: Publisher,
) -> None:
    """Руководитель решает по ссылке, которую предложил менеджер."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    _, action, fid = callback.data.split(":", 2)
    proposal = app_state.file_proposal(fid)
    await _clear_markup(callback)
    if proposal is None:
        await callback.answer("Это предложение уже неактуально", show_alert=True)
        return
    app_state.drop_file_proposal(fid)
    by_id = proposal.get("by_id")

    if action == "no":
        await callback.answer("Отклонено")
        if by_id:
            await _notify_manager(
                callback.bot, by_id,
                f"Руководитель посмотрел ссылку {proposal['url']} — в список не берём.",
            )
        return

    title = proposal.get("title") or ""
    ok, note = await asyncio.to_thread(
        links.add, title, proposal["url"], [],
        f"предложил {proposal.get('by') or 'менеджер'}",
    )
    await callback.answer("Сохранил" if ok else "Не сохранил")
    if ok:
        git_ok, git_msg = await asyncio.to_thread(
            publisher.commit_paths, [links_module.REL_PATH],
            f"Ссылки (бот): {title or proposal['url']}, предложил {proposal.get('by') or ''}",
        )
        note += "" if git_ok else f"\n\n⚠️ В git не ушло: {git_msg}"
    await callback.message.answer(note)
    if by_id:
        await _notify_manager(
            callback.bot, by_id,
            f"Ссылку {proposal['url']} " + ("добавили в полезное — она уже отдаётся по запросу."
                                            if ok else f"не добавили: {note}"),
        )


@own.callback_query(F.data.startswith("fprop:"))
async def on_file_proposal(
    callback: CallbackQuery, config: Config, app_state: State, files: FileLibrary
) -> None:
    """Руководитель решает по файлу, который предложил менеджер."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    _, action, fid = callback.data.split(":", 2)
    proposal = app_state.file_proposal(fid)
    await _clear_markup(callback)
    if proposal is None:
        await callback.answer("Это предложение уже неактуально", show_alert=True)
        return
    app_state.drop_file_proposal(fid)
    staged = Path(proposal["staged"])
    by_id = proposal.get("by_id")

    if action == "no":
        staged.unlink(missing_ok=True)
        await callback.answer("Отклонено")
        if by_id:
            await _notify_manager(
                callback.bot, by_id,
                f"Руководитель посмотрел файл «{proposal['original_name']}» — в библиотеку не берём.",
            )
        return

    if not staged.is_file():
        await callback.answer("Файл не сохранился — попроси прислать заново", show_alert=True)
        return
    # Название — первая строка слов менеджера, теги пустые.
    note = (proposal.get("note") or "").strip()
    title = note.splitlines()[0][:90] if note else proposal["original_name"]
    pending = PendingUpload(
        staged=staged, original_name=proposal["original_name"], ext=proposal["ext"],
    )
    try:
        file_id, _ = await asyncio.to_thread(
            uploads.finalize, files.dir, pending, title, [],
            f"{note}\nПредложил: {proposal.get('by') or 'менеджер'}",
        )
        files.reload()
    except Exception:
        log.exception("Не смог сохранить предложенный файл %s", fid)
        await callback.answer("Не смог сохранить файл", show_alert=True)
        return
    await callback.answer("В библиотеке")
    await callback.message.answer(
        f"Файл добавлен: <b>{to_html(title)}</b>\nid: <code>{file_id}</code>\n"
        f"Теги не заданы — можно дописать позже. В git уйдёт при ближайшей "
        f"публикации или командой /publish.",
        parse_mode="HTML",
    )
    if by_id:
        await _notify_manager(
            callback.bot, by_id,
            f"Файл «{proposal['original_name']}» приняли — он в библиотеке, "
            f"теперь его можно запросить у меня словами.",
        )


# --- Сборка роутеров -------------------------------------------------------
# Порядок включения = порядок поиска обработчика в aiogram: сначала модули с конкретными
# командами и кнопками, последним — `own` с «ловящими всё» `on_question` и
# `on_group_message`. Пункты меню регистрируем здесь, а не импортом в h_menu, — иначе кольцо.
h_menu.register(
    changes=on_period_report,
    state=on_state_map,
    journal=on_journal,
    hypotheses=on_hypotheses,
    links=on_links,
    files=on_files,
    chats=on_chats,
    svodka=on_svodka,
    publish=on_publish,
    otzyvy=on_otzyvy,
    idei=on_idei,
    voprosy=on_top_questions,
    model=on_model,
    stats=on_stats,
    compare=on_compare,
    pending=on_pending_facts,
)

router.include_router(h_menu.router)
router.include_router(h_edit.router)
router.include_router(own)
