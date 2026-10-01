"""Правка базы знаний через бота: от «обнови базу: …» до коммита в git.

В базу ничего не пишется, пока автор правки не увидел diff и не нажал «Внести».
Любая запись идёт под `KB_WRITE_LOCK`: сверить с диском → записать → git → перечитать.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import autonomy
import kb_write
from access import deny as _deny, resolve_role, role_of as _role
from botstate import (
    EditContext,
    KB_WRITE_LOCK,
    ManagerEditRequest,
    PENDING_CREATE_ASK,
    PENDING_EDITS,
    PENDING_MANAGER_EDITS,
    PENDING_NEW_UNITS,
    next_pending_id as _next_pending_id,
    take_pending as _take_pending,
)
from answer_cache import AnswerCache
from config import Config
from formatting import to_html
from kb import KnowledgeBase
from kb_editor import NO_FITTING_UNIT, EditProposal, KbEditor
from menu import MENU_KEYBOARD
from publisher import Publisher
from state import State
from ui import (
    clear_markup as _clear_markup,
    clip_text as _clip_text,
    keep_typing as _keep_typing,
    notify_manager as _notify_manager,
    notify_leaders,
    send_block as _send_block,
    send_long as _send_long,
    trim_dict as _trim_dict,
)

log = logging.getLogger(__name__)
router = Router()

PENDING_PAGE = 8

DIFF_MAX_LINES = 200


def _write_atomic(path: Path, text: str) -> None:
    """Запись через временный файл и `os.replace`: файл на диске никогда не бывает неполным."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@router.message(Command("update"), F.chat.type == ChatType.PRIVATE)
async def on_update(
    message: Message, config: Config, app_state: State, kb_editor: KbEditor
) -> None:
    """Человек описывает правку базы своими словами — бот готовит, показывает, ждёт «да»."""
    role = _role(config, message, app_state)
    if not role:
        await _deny(message, config)
        return
    instruction = (message.text or "").partition(" ")[2].strip()
    if not instruction:
        await message.answer(
            "Опиши правку своими словами, например:\n"
            "<code>/update минимальный срок договора теперь три месяца, а не один</code>\n"
            "Я найду нужную единицу, покажу изменение и спрошу подтверждение.",
            parse_mode="HTML",
        )
        return

    await run_update(
        message.bot, message.chat.id, message.from_user.id,
        app_state, kb_editor, instruction,
        author=message.from_user.full_name, leader=(role == "leader"),
    )


async def run_update(
    bot: Bot, chat_id: int, owner_id: int,
    app_state: State, kb_editor: KbEditor, instruction: str,
    attribution: str | None = None, attribution_id: int | None = None,
    origin: str | None = None, fact_id: str | None = None,
    author: str = "", leader: bool = False,
) -> None:
    """Готовит правку и показывает её автору (`owner_id`) с кнопками «Внести / Отмена»."""
    await bot.send_message(chat_id, "Смотрю базу, ищу, куда это ложится…")
    typing = asyncio.create_task(_keep_typing(bot, chat_id))
    try:
        candidates, note = await kb_editor.pick_units(instruction, model=app_state.model)
    except Exception:
        log.exception("Не смог подобрать единицу для правки")
        await bot.send_message(chat_id, "Не смог подготовить правку. Попробуй переформулировать.")
        if fact_id:
            app_state.release_digest_fact(fact_id)
        if attribution_id is not None:
            await _notify_manager(
                bot, attribution_id,
                "Руководитель взял твоё предложение по базе, но подготовить правку не вышло — "
                "он уточнит детали позже.",
            )
        return
    finally:
        typing.cancel()

    ask_id = _next_pending_id()
    context = EditContext(
        instruction=instruction,
        attribution=attribution,
        attribution_id=attribution_id,
        origin=origin,
        reasons={c.kb_id: c.reason for c in candidates if c.reason},
        fact_id=fact_id,
        author=author,
        leader=leader,
    )
    PENDING_CREATE_ASK[ask_id] = (owner_id, context)
    _trim_dict(PENDING_CREATE_ASK, limit=200)

    if not candidates:
        if note == NO_FITTING_UNIT:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Создать новую", callback_data=f"newunit:go:{ask_id}"),
                InlineKeyboardButton(text="❌ Не надо", callback_data=f"newunit:cancel:{ask_id}"),
            ]])
            await bot.send_message(
                chat_id,
                "Ни одна существующая единица под это не подходит.\n"
                "Создать <b>новую единицу</b> базы? Сам подберу раздел и напишу текст — "
                "покажу целиком перед записью.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        PENDING_CREATE_ASK.pop(ask_id, None)
        await bot.send_message(chat_id, note)
        if fact_id:
            app_state.release_digest_fact(fact_id)
        if attribution_id is not None:
            await _notify_manager(
                bot, attribution_id,
                "Руководитель посмотрел твоё предложение, но бот не смог сам подобрать правку "
                f"({note}) — руководитель разберётся вручную.",
            )
        return

    if len(candidates) == 1:
        await _prepare_edit(bot, chat_id, ask_id, app_state, kb_editor, candidates[0].kb_id)
        return

    def _label(c) -> str:
        return f"{c.kb_id} · {c.title[:40]}" if leader else c.title[:56]

    rows = [
        [InlineKeyboardButton(text=_label(c), callback_data=f"pick:{ask_id}:{c.kb_id}")]
        for c in candidates
    ]
    rows.append([InlineKeyboardButton(text="➕ Лучше новая тема", callback_data=f"newunit:go:{ask_id}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"newunit:cancel:{ask_id}")])
    listing = "\n".join(
        (f"• <b>{c.kb_id}</b> — {to_html(c.title)}" if leader else f"• <b>{to_html(c.title)}</b>")
        + (f"\n   <i>{to_html(c.reason)}</i>" if c.reason else "")
        for c in candidates
    )
    await bot.send_message(
        chat_id,
        f"Подходит несколько тем — выбери, куда внести:\n\n{listing}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _prepare_edit(
    bot: Bot, chat_id: int, ask_id: int, app_state: State,
    kb_editor: KbEditor, kb_id: str,
) -> None:
    """Готовит и показывает diff по выбранной единице (с перепроверкой лишнего)."""
    pending = PENDING_CREATE_ASK.get(ask_id)
    if pending is None:
        await bot.send_message(chat_id, "Правка уже неактуальна — начни заново.")
        return
    owner_id, ctx = pending
    attribution, attribution_id, origin = ctx.attribution, ctx.attribution_id, ctx.origin

    await bot.send_message(chat_id, "Готовлю запись и перепроверяю её…")
    typing = asyncio.create_task(_keep_typing(bot, chat_id))
    try:
        proposal, note = await kb_editor.prepare(kb_id, ctx.instruction, model=app_state.model)
    except Exception:
        log.exception("Не смог подготовить правку %s", kb_id)
        await bot.send_message(chat_id, "Не смог подготовить правку. Попробуй переформулировать.")
        if ctx.fact_id:
            app_state.release_digest_fact(ctx.fact_id)
        return
    finally:
        typing.cancel()

    if proposal is None:
        await bot.send_message(chat_id, note)
        if ctx.fact_id:
            app_state.release_digest_fact(ctx.fact_id)
        if attribution_id is not None:
            await _notify_manager(
                bot, attribution_id,
                f"Руководитель взял твоё предложение, но правка не собралась ({note}).",
            )
        return

    unit = kb_editor.kb.get(kb_id)

    # Строки для показа человеку берутся до служебной аннотации.
    removed, added = autonomy.changed_lines(proposal.old_text, proposal.new_text)
    added = [ln for ln in added if not ln.startswith(("updated:", "status:"))]
    removed = [ln for ln in removed if not ln.startswith(("updated:", "status:"))]
    prov = {
        "who": ctx.author or str(owner_id),
        "who_id": owner_id,
        "msg": origin or "личка с ботом",
        "said": date.today().isoformat(),
        "signed": True,
    }
    proposal.new_text = autonomy.annotate_claim(
        proposal.old_text, proposal.new_text, prov, "confirmed"
    )

    PENDING_CREATE_ASK.pop(ask_id, None)
    proposal.proposed_by = attribution
    proposal.proposed_by_id = attribution_id
    proposal.origin = origin
    proposal.fact_id = ctx.fact_id
    edit_id = _next_pending_id()
    PENDING_EDITS[edit_id] = (owner_id, proposal)
    _trim_dict(PENDING_EDITS, limit=100)

    warn = ""
    if proposal.state_change:
        state_key, was, became = proposal.state_change
        warn += (
            f"\n\n🔄 <b>Обновляю состояние</b> — {to_html(state_key)}:\n"
            + (f"   было: {to_html(was)}\n" if was else "   (такой строки ещё не было)\n")
            + f"   стало: {to_html(became)}"
            + ("\n   Старое значение уедет в «Было раньше», не пропадёт." if was else "")
        )
    if proposal.shrinks_a_lot():
        warn += "\n⚠️ Единица заметно укоротилась — проверь, не потерялось ли лишнее!"
    if not proposal.audited:
        warn += (
            "\n⚠️ <b>Перепроверка не сработала</b> — правка не проверена на постороннее. "
            "Читай diff особенно внимательно."
        )
    if proposal.reverted:
        shown = "\n".join(f"   — {to_html(item)}" for item in proposal.reverted[:5])
        more = "" if len(proposal.reverted) <= 5 else f"\n   … и ещё {len(proposal.reverted) - 5}"
        warn += (
            f"\n\n🛡 <b>Перепроверка откатила лишнее</b> ({len(proposal.reverted)}): "
            f"модель тронула текст не по делу, я вернул как было.\n{shown}{more}"
        )
    diff_text, cut = proposal.diff_full(DIFF_MAX_LINES)
    cut_note = (
        f"\n⚠️ Показал не весь diff (ещё {cut} строк). Правка большая — "
        f"проверь единицу целиком в git перед подтверждением."
        if cut else ""
    )
    rows = [[
        InlineKeyboardButton(
            text="✅ Внести" if ctx.leader else "✅ Верно, записать",
            callback_data=f"edit:apply:{edit_id}",
        ),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit:cancel:{edit_id}"),
    ]]
    waiting = sum(1 for uid, _p in PENDING_EDITS.values() if uid == owner_id)
    if waiting > 1:
        rows.append([
            InlineKeyboardButton(
                text=f"✅✅ Внести все {waiting}", callback_data="edit:all",
            )
        ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    signed = (
        f"\n\n<i>Под записью будет стоять: внёс {to_html(ctx.author or str(owner_id))}, "
        f"сегодняшняя дата.</i>"
    )
    if ctx.leader:
        await _send_block(
            bot, chat_id,
            f"<b>Правка {proposal.kb_id}</b>: {to_html(proposal.summary)}\n"
            + _edit_context_block(ctx, proposal, kb_id)
            + f"{warn}{cut_note}{signed}",
            diff_text,
            keyboard,
        )
        return
    title = getattr(unit, "title", "") or proposal.kb_id
    body = [f"+ {ln}" for ln in added]
    gone = ""
    # При замене состояния прежнее значение уже показано фразой «было → стало».
    if removed and not proposal.state_change:
        body = [f"− {ln}" for ln in removed] + body
        gone = (
            "\n⚠️ Часть прежнего текста будет убрана или заменена — это строки с «−». "
            "Проверь, что так и задумано."
        )
    await _send_block(
        bot, chat_id,
        f"📝 <b>Запишу в тему «{to_html(title)}»</b> — вот так:{warn}{gone}{signed}\n"
        "<i>Верно? Не так — нажми «Отмена» и напиши точнее.</i>",
        "\n".join(body) or proposal.summary,
        keyboard,
    )


def _edit_context_block(ctx: EditContext, proposal: EditProposal, kb_id: str) -> str:
    """Контекст правки: откуда взялось, почему сюда, что меняется."""
    lines = []
    if ctx.origin:
        lines.append(f"📎 <i>{to_html(ctx.origin)}</i>")
    quote = " ".join((ctx.instruction or "").split())
    if quote:
        who = f" — {to_html(ctx.attribution)}" if ctx.attribution else ""
        lines.append(f"💬 «{to_html(_clip_text(quote, 300))}»{who}")
    reason = ctx.reasons.get(kb_id)
    if reason:
        lines.append(f"🎯 Почему {kb_id}: <i>{to_html(reason)}</i>")
    changes, extra = proposal.change_lines()
    if changes:
        shown = "\n".join(f"   • {to_html(c)}" for c in changes)
        tail = f"\n   … и ещё {extra} мест" if extra else ""
        lines.append(f"✏️ <b>Что меняется:</b>\n{shown}{tail}")
    return ("\n".join(lines) + "\n") if lines else ""


@router.message(Command("hvosty"), F.chat.type == ChatType.PRIVATE)
async def on_pending_facts(
    message: Message, config: Config, app_state: State
) -> None:
    """Пункты сводок, по которым не приняли решение."""
    if _role(config, message, app_state) != "leader":
        await message.answer("Это только для руководителя.")
        return
    pending = app_state.pending_digest_facts()
    if not pending:
        await message.answer("Неразобранных пунктов из сводок нет — всё чисто.")
        return

    await message.answer(
        f"<b>Неразобранные пункты из сводок: {len(pending)}</b>\n"
        f"Это то, что бот услышал в чатах и не записал в базу — ждёт решения.\n"
        f"Сначала те, что повторяли в чатах, потом свежие. По {PENDING_PAGE} штук.",
        parse_mode="HTML",
    )
    for fact_id, fact in pending[:PENDING_PAGE]:
        when = (fact.get("at") or "")[:10]
        chat = fact.get("chat") or "рабочий чат"
        repeats = int(fact.get("repeats") or 1)
        also = ", ".join(fact.get("also") or [])
        mark = (
            f"\n🔁 <i>упоминали {repeats} раз(а)"
            + (f": {to_html(also)}" if also else "")
            + "</i>"
            if repeats > 1 else ""
        )
        await message.answer(
            f"<b>{when}</b> · {to_html(chat)}{mark}\n{to_html(fact.get('text', ''))}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Внести", callback_data=f"dg:{fact_id}"),
                InlineKeyboardButton(text="🚫 Не надо", callback_data=f"dgskip:{fact_id}"),
            ]]),
        )
    if len(pending) > PENDING_PAGE:
        await message.answer(
            f"…и ещё {len(pending) - PENDING_PAGE}. Разбери эти — покажу следующие "
            f"по <code>/hvosty</code>.",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("dgskip:"))
async def on_digest_skip(
    callback: CallbackQuery, config: Config, app_state: State
) -> None:
    """«Не надо» — пункт разобран, в базу не идёт."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    fact_id = callback.data.split(":", 1)[1]
    app_state.close_digest_fact(fact_id, "не надо")
    await callback.answer("Ок, закрыл")
    await _clear_markup(callback)


@router.callback_query(F.data.startswith("dg:"))
async def on_digest_add(
    callback: CallbackQuery, config: Config, app_state: State, kb_editor: KbEditor
) -> None:
    """«Внести» под пунктом сводки — запускает обычный путь правки с подтверждением."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    fact_id = callback.data.split(":", 1)[1]
    fact = app_state.digest_fact(fact_id)
    if fact is None:
        await callback.answer(
            "Этот пункт уже не найду — сводка была давно. Пришли текстом: «обнови базу: …»",
            show_alert=True,
        )
        return
    # Пункт берётся в работу, а не закрывается: закроется по факту записи.
    if not app_state.claim_digest_fact(fact_id):
        await callback.answer("Этот пункт уже в работе — жду решения по готовой правке", show_alert=True)
        return
    await callback.answer("Готовлю правку")
    origin = f"сводка по чатам, {fact.get('chat') or 'рабочий чат'}"
    await run_update(
        callback.bot, callback.message.chat.id, callback.from_user.id,
        app_state, kb_editor, fact["text"], origin=origin, fact_id=fact_id,
        author=callback.from_user.full_name, leader=True,
    )


async def _apply_batch(
    callback: CallbackQuery, app_state: State, kb: KnowledgeBase,
    publisher: Publisher, answer_cache: AnswerCache,
) -> None:
    """Вносит разом все правки этого человека; вторую правку в ту же единицу возвращает в хвосты."""
    owner = callback.from_user.id
    mine = [(key, prop) for key, (uid, prop) in PENDING_EDITS.items() if uid == owner]
    if not mine:
        await callback.answer("Нечего вносить — все правки уже разобраны", show_alert=True)
        return

    await callback.answer(f"Вношу {len(mine)}…")
    await _clear_markup(callback)
    done: list[str] = []
    skipped: list[str] = []
    seen_units: set[str] = set()
    for key, proposal in mine:
        if proposal.kb_id in seen_units:
            skipped.append(f"{proposal.kb_id}: вторая правка в ту же единицу")
            PENDING_EDITS.pop(key, None)
            if proposal.fact_id:
                app_state.release_digest_fact(proposal.fact_id)
            continue
        seen_units.add(proposal.kb_id)
        PENDING_EDITS.pop(key, None)
        message_text = (
            f"База (бот): {proposal.summary} [{proposal.kb_id}], "
            f"подтвердил {owner} (пакетом)"
        )
        async with KB_WRITE_LOCK:
            result = await asyncio.to_thread(
                kb_write.apply_edit, kb, publisher, proposal, message_text
            )
            if result.wrote:
                kb.reload()
                answer_cache.sync(kb.unit_hashes())
        if result.wrote and proposal.fact_id:
            app_state.close_digest_fact(proposal.fact_id, "внесён")
        elif proposal.fact_id:
            app_state.release_digest_fact(proposal.fact_id)
        if result.ok:
            done.append(f"{proposal.kb_id} — {proposal.summary}")
        elif result.wrote:
            done.append(f"{proposal.kb_id} — записал, но git не прошёл: {result.message}")
        else:
            skipped.append(f"{proposal.kb_id}: {result.message}")

    lines = [f"✅ <b>Внесено пачкой: {len(done)}</b>"]
    lines += [f"• {to_html(item)}" for item in done]
    if skipped:
        lines.append("")
        lines.append(f"⚠️ <b>Не внесено ({len(skipped)}) — вернул в хвосты:</b>")
        lines += [f"• {to_html(item)}" for item in skipped]
        lines.append("<i>Они снова в /hvosty — там правка пересоберётся на свежем тексте.</i>")
    await _send_long(callback.bot, callback.message.chat.id, "\n".join(lines))


@router.message(Command("avtonom"), F.chat.type == ChatType.PRIVATE)
async def on_autonomy(message: Message, config: Config, app_state: State) -> None:
    """Стоп-кран автономного пополнения: `/avtonom on|off`, без аргумента — статус."""
    if _role(config, message, app_state) != "leader":
        await _deny(message, config)
        return
    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg in {"on", "вкл", "включить", "1"}:
        app_state.set_autonomy(True)
    elif arg in {"off", "выкл", "выключить", "0"}:
        app_state.set_autonomy(False)
    elif arg:
        await message.answer("Не понял. Пиши «/avtonom on» или «/avtonom off».")
        return
    today = app_state.auto_edits_today()
    state_text = "включено" if app_state.autonomy else "выключено"
    await message.answer(
        f"<b>Автономное пополнение: {state_text}.</b>\n"
        f"Сегодня бот внёс сам: {today} из {autonomy.DAILY_LIMIT}.\n\n"
        f"🟢 факты с датой и источником и 🟡 уточнения правил — пишет сам, ты видишь "
        f"их в ближайшей сводке с кнопкой «Откатить».\n"
        f"🔴 цифры, деньги, слова клиенту, гипотезы, снятие needs-check — готовит, "
        f"но ждёт твоего «Внести». Удалять текст базы не может вообще.\n\n"
        f"<i>Переключить: /avtonom on · /avtonom off</i>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("undo:"))
async def on_undo_auto(
    callback: CallbackQuery, config: Config, app_state: State, auto_writer,
) -> None:
    """«Откатить» под автоправкой: git revert ровно этого коммита."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    sha = callback.data.split(":", 1)[1]
    if auto_writer is None:
        await callback.answer("Автономный режим не собран — откатывать нечем", show_alert=True)
        return
    await callback.answer("Откатываю…")
    ok, note = await auto_writer.rollback(sha, reason="решение руководителя")
    await callback.message.reply(
        f"↩️ Откатил автоправку {sha[:8]} — база вернулась к прежней версии."
        if ok else f"Не смог откатить {sha[:8]}: {note}"
    )


@router.callback_query(F.data.startswith("pick:"))
async def on_pick_unit(
    callback: CallbackQuery, config: Config, app_state: State, kb_editor: KbEditor
) -> None:
    """Автор правки выбрал, в какую из подходящих единиц её вносить."""
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, raw_ask, kb_id = callback.data.split(":", 2)
    await _clear_markup(callback)
    pending = PENDING_CREATE_ASK.get(int(raw_ask)) if raw_ask.isdigit() else None
    if pending is None or pending[0] != callback.from_user.id:
        await callback.answer("Этот запрос уже неактуален")
        return
    await callback.answer(f"Готовлю правку {kb_id}")
    await _prepare_edit(
        callback.bot, callback.message.chat.id, int(raw_ask), app_state, kb_editor, kb_id,
    )


async def queue_manager_proposal(
    bot: Bot, config: Config, user, instruction: str, origin: str | None = None
) -> str:
    """Ставит предложение менеджера в очередь руководителю; возвращает текст ответа менеджеру."""
    if not config.leaders:
        return "Некому отправить предложение на подтверждение — напиши руководителю напрямую."

    request_id = _next_pending_id()
    PENDING_MANAGER_EDITS[request_id] = ManagerEditRequest(
        request_id=request_id,
        manager_id=user.id,
        manager_name=user.full_name,
        manager_username=user.username,
        instruction=instruction,
        origin=origin,
    )
    _trim_dict(PENDING_MANAGER_EDITS, limit=200)

    who = f"@{user.username}" if user.username else user.full_name
    src = f"\nИсточник: <i>{to_html(origin)}</i>" if origin else ""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готовить правку", callback_data=f"medit:go:{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"medit:no:{request_id}"),
    ]])
    sent = await notify_leaders(
        bot,
        config,
        f"✏️ <b>Предложение правки базы</b>\n"
        f"От: {to_html(who)} (id {user.id}){src}\n\n"
        f"«{to_html(instruction)}»\n\n"
        f"Готовить правку? Покажу diff перед записью — сам в базу ничего не внесу.",
        keyboard,
    )
    if not sent:
        PENDING_MANAGER_EDITS.pop(request_id, None)
        return "Не смог доставить предложение руководителю — напиши руководителю напрямую."
    log.info("Предложение правки #%s от %s (%s): %s", request_id, user.id, who, instruction[:200])
    return "Отправил на подтверждение руководителю. Как решит — сообщу тебе."






async def submit_manager_proposal(
    message: Message, config: Config, instruction: str, origin: str | None = None
) -> None:
    reply = await queue_manager_proposal(
        message.bot, config, message.from_user, instruction, origin
    )
    await message.answer(reply, reply_markup=MENU_KEYBOARD)


@router.callback_query(F.data.startswith("medit:"))
async def on_manager_edit_decision(
    callback: CallbackQuery, config: Config, app_state: State, kb_editor: KbEditor
) -> None:
    """Руководитель решает по предложению менеджера: готовить правку или отклонить."""
    if config.role(callback.from_user.id, callback.from_user.username) != "leader":
        await callback.answer("Только для руководителя", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":", 2)
    req = PENDING_MANAGER_EDITS.pop(int(raw_id), None)
    await _clear_markup(callback)
    if req is None:
        await callback.answer("Запрос уже неактуален")
        return

    who = f"@{req.manager_username}" if req.manager_username else req.manager_name
    if action == "no":
        await callback.answer("Отклонено")
        await callback.message.reply(f"Отклонено. Предложение от {who} в базу не пойдёт.")
        await _notify_manager(
            callback.bot, req.manager_id,
            "Руководитель посмотрел твоё предложение по базе и решил пока не вносить. "
            "Если это важно — уточни детали и предложи снова.",
        )
        return

    await callback.answer("Готовлю правку…")
    await run_update(
        callback.bot, callback.message.chat.id, callback.from_user.id,
        app_state, kb_editor, req.instruction,
        attribution=who, attribution_id=req.manager_id, origin=req.origin,
        author=callback.from_user.full_name, leader=True,
    )


@router.callback_query(F.data.startswith("newunit:"))
async def on_new_unit_ask(
    callback: CallbackQuery, config: Config, app_state: State, kb_editor: KbEditor
) -> None:
    """Создавать ли новую единицу под этот апдейт."""
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    pending = _take_pending(PENDING_CREATE_ASK, parts[2] if len(parts) > 2 else "", callback.from_user.id)
    await _clear_markup(callback)
    if pending is None:
        await callback.answer("Запрос уже неактуален")
        return
    # _take_pending отдаёт запись без owner_id — он уже сверен.
    ctx: EditContext = pending
    instruction, attribution = ctx.instruction, ctx.attribution
    attribution_id, origin = ctx.attribution_id, ctx.origin

    if action == "cancel":
        await callback.answer("Отменено")
        await callback.message.reply("Ок, новую единицу не создаю. База не изменена.")
        if attribution_id is not None:
            await _notify_manager(
                callback.bot, attribution_id,
                "Руководитель посмотрел твоё предложение: под него нет подходящей единицы, "
                "и новую решили пока не создавать.",
            )
        return

    await callback.answer("Готовлю новую единицу…")
    await callback.message.reply("Пишу новую единицу, подбираю раздел…")
    typing = asyncio.create_task(_keep_typing(callback.bot, callback.message.chat.id))
    author = attribution or ctx.author or "руководителя"
    try:
        proposal, note = await kb_editor.propose_new(
            instruction, author=author, model=app_state.model, origin=origin
        )
    except Exception:
        log.exception("Не смог подготовить новую единицу")
        await callback.message.reply("Не смог собрать новую единицу. Попробуй переформулировать.")
        return
    finally:
        typing.cancel()

    if proposal is None:
        await callback.message.reply(note)
        return

    prov = {
        "who": ctx.author or callback.from_user.full_name,
        "who_id": callback.from_user.id,
        "msg": origin or "личка с ботом",
        "said": date.today().isoformat(),
        "signed": True,
    }
    proposal.text = (
        proposal.text.rstrip() + "\n\n" + autonomy.signature(prov).strip() + "\n\n"
        + autonomy.claim_comment(prov, "confirmed") + "\n"
    )

    proposal.proposed_by = attribution
    proposal.proposed_by_id = attribution_id
    unit_pending_id = _next_pending_id()
    PENDING_NEW_UNITS[unit_pending_id] = (callback.from_user.id, proposal)
    _trim_dict(PENDING_NEW_UNITS, limit=100)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Создать", callback_data=f"newapply:yes:{unit_pending_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"newapply:no:{unit_pending_id}"),
    ]])
    origin_line = f"📎 <i>{to_html(origin)}</i>\n" if origin else ""
    quote = " ".join((instruction or "").split())
    quote_line = (
        f"💬 «{to_html(_clip_text(quote, 300))}»"
        + (f" — {to_html(attribution)}" if attribution else "")
        + "\n"
        if quote else ""
    )
    await _send_block(
        callback.bot, callback.message.chat.id,
        f"<b>Новая единица {proposal.kb_id}</b> — раздел {proposal.section}, "
        f"тип {proposal.unit_type}\n"
        f"{origin_line}{quote_line}"
        f"Файл: <code>{proposal.rel_path}</code>\n"
        f"В каталог INDEX.md строка добавится автоматически.",
        proposal.preview(body_lines=200),
        keyboard,
    )


@router.callback_query(F.data.startswith("newapply:"))
async def on_new_unit_apply(
    callback: CallbackQuery, config: Config, app_state: State, kb: KnowledgeBase,
    publisher: Publisher, answer_cache: AnswerCache,
) -> None:
    """Подтверждение записи новой единицы: файл + строка в INDEX одним коммитом."""
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    proposal = _take_pending(
        PENDING_NEW_UNITS, parts[2] if len(parts) > 2 else "", callback.from_user.id
    )
    await _clear_markup(callback)
    if proposal is None:
        await callback.answer("Единица уже неактуальна")
        return
    if action == "no":
        await callback.answer("Отменено")
        await callback.message.reply("Создание отменено, база не изменена.")
        if proposal.proposed_by_id is not None:
            await _notify_manager(
                callback.bot, proposal.proposed_by_id,
                "Руководитель посмотрел новую единицу по твоему предложению и решил её не создавать.",
            )
        return

    await callback.answer("Создаю…")
    proposed = f" — предложил {proposal.proposed_by}" if proposal.proposed_by else ""
    if proposal.origin:
        proposed += f" ({proposal.origin})"

    def _apply() -> tuple[str, str]:
        result = kb_write.apply_new_unit(
            kb, publisher, proposal,
            f"База (бот): новая единица {proposal.kb_id} «{proposal.title}»{proposed}, "
            f"подтвердил {callback.from_user.full_name}",
        )
        return result.stage, result.message

    async with KB_WRITE_LOCK:
        stage, msg = await asyncio.to_thread(_apply)
        if stage != "нетронуто":
            kb.reload()
            # Сброс кэша целиком: ответ «в базе этого нет» ни на какую единицу не ссылается.
            answer_cache.clear(kb.unit_hashes(), f"создана {proposal.kb_id}")

    if stage == "закоммичено":
        await callback.message.reply(
            f"✅ Создана {proposal.kb_id} «{proposal.title}», добавлена в каталог "
            f"и закоммичена. В базе теперь {len(kb.units)} единиц."
        )
        if proposal.proposed_by_id is not None:
            await _notify_manager(
                callback.bot, proposal.proposed_by_id,
                f"По твоему предложению создана новая единица базы {proposal.kb_id} "
                f"«{proposal.title}». Спасибо!",
            )
    elif stage == "записано":
        await callback.message.reply(
            f"⚠️ Единица {proposal.kb_id} создана на диске и бот её уже видит "
            f"({len(kb.units)} единиц), но в git не уехала: {msg}\n"
            f"Нужно дожать коммит вручную."
        )
    else:
        await callback.message.reply(f"Ничего не записал: {msg}")


@router.callback_query(F.data.startswith("edit:"))
async def on_edit_decision(
    callback: CallbackQuery, config: Config, app_state: State,
    kb: KnowledgeBase, publisher: Publisher, answer_cache: AnswerCache,
) -> None:
    if not resolve_role(config, app_state, callback.from_user.id, callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "all":
        await _apply_batch(callback, app_state, kb, publisher, answer_cache)
        return
    proposal = _take_pending(
        PENDING_EDITS, parts[2] if len(parts) > 2 else "", callback.from_user.id
    )
    await _clear_markup(callback)
    if proposal is None:
        await callback.answer("Правка уже неактуальна")
        return
    if action == "cancel":
        await callback.answer("Отменено")
        await callback.message.reply("Правка отменена, база не изменена.")
        if proposal.fact_id:
            app_state.release_digest_fact(proposal.fact_id)
        if proposal.proposed_by_id is not None:
            await _notify_manager(
                callback.bot, proposal.proposed_by_id,
                "Руководитель посмотрел готовую правку по твоему предложению и решил её не вносить.",
            )
        return

    await callback.answer("Вношу…")
    proposed = f" — предложил {proposal.proposed_by}" if proposal.proposed_by else ""
    if proposal.origin:
        proposed += f" ({proposal.origin})"

    # Возвращает стадию: «нетронуто» / «записано» / «закоммичено».
    def _apply() -> tuple[str, str]:
        path = kb.root / proposal.rel_path
        # Сверка с диском: файл мог измениться после подготовки правки (ручная правка, pull).
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as error:
            return "нетронуто", f"не смог прочитать {proposal.rel_path}: {error}"
        if current != proposal.old_text:
            return "нетронуто", (
                f"{proposal.kb_id} изменилась с момента подготовки правки "
                f"(кто-то поправил файл или подтянулись чужие коммиты). "
                f"Ничего не записал — повтори правку, я пересоберу её на свежей версии."
            )
        _write_atomic(path, proposal.new_text)
        ok, msg = publisher.commit_paths(
            [proposal.rel_path],
            f"База (бот): {proposal.summary} [{proposal.kb_id}]{proposed}, "
            f"подтвердил {callback.from_user.full_name}",
        )
        return ("закоммичено" if ok else "записано"), msg

    async with KB_WRITE_LOCK:
        stage, msg = await asyncio.to_thread(_apply)
        # Перечитать базу сразу после записи, не дожидаясь git: иначе на диске новое, в памяти старое.
        if stage != "нетронуто":
            kb.reload()
            answer_cache.sync(kb.unit_hashes())

    if proposal.fact_id:
        if stage != "нетронуто":
            app_state.close_digest_fact(proposal.fact_id, "внесён")
        else:
            app_state.release_digest_fact(proposal.fact_id)

    if stage == "закоммичено":
        await callback.message.reply(
            f"✅ Внесено в {proposal.kb_id} и закоммичено в git. База обновлена."
        )
        if proposal.proposed_by_id is not None:
            await _notify_manager(
                callback.bot, proposal.proposed_by_id,
                f"Твоё предложение внесено в базу ({proposal.kb_id}). Спасибо!",
            )
    elif stage == "записано":
        await callback.message.reply(
            f"⚠️ Правка в файл записана и бот её уже видит, но в git не уехала: {msg}\n"
            f"Файл на сервере верный — нужно дожать коммит вручную."
        )
    else:
        await callback.message.reply(f"Ничего не записал: {msg}")

