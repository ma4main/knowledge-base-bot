"""Отправка сообщений в Telegram: разбиение, разметка, кнопки, «печатает».

Telegram отвергает целиком (а не обрезает) сообщение длиннее 4096 символов и
сообщение с битой разметкой. Поэтому длинный текст режется по абзацам, а на
отвергнутую разметку есть запасной путь — то же самое без тегов.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)

from formatting import split_message, to_html

log = logging.getLogger(__name__)


async def send(
    message: Message,
    text: str,
    already_html_head: bool = False,
    markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Отправляет ответ (возможно несколькими кусками). Возвращает последнее сообщение."""
    chunks = split_message(text)
    sent: Message | None = None
    for index, chunk in enumerate(chunks):
        # В режиме сравнения первая строка уже размечена — её экранировать нельзя.
        if already_html_head and index == 0:
            head, _, rest = chunk.partition("\n\n")
            body = f"{head}\n\n{to_html(rest)}"
        else:
            body = to_html(chunk)
        # Кнопки («Подробнее», оценка) — только под последним куском: они относятся
        # к ответу целиком.
        markup_here = markup if index == len(chunks) - 1 else None
        try:
            sent = await message.answer(body, parse_mode="HTML", reply_markup=markup_here)
        except Exception:
            log.warning("HTML-разметка отвергнута Telegram, шлю без неё", exc_info=True)
            sent = await message.answer(chunk, reply_markup=markup_here)
    return sent


async def send_long(
    bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    """Отправляет длинный HTML-текст, разбивая по лимиту Telegram; кнопки — под последним куском."""
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        markup = keyboard if index == len(chunks) - 1 else None
        try:
            await bot.send_message(chat_id, chunk, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log.warning("HTML отвергнут Telegram, шлю без разметки", exc_info=True)
            await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", chunk), reply_markup=markup)


async def send_block(
    bot: Bot, chat_id: int, header_html: str, body: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Заголовок + длинный текст блоком (diff, превью единицы), с разбиением.

    Режем сырой текст и оформляем каждый кусок отдельно: разрез готового HTML
    попадает в середину тега, и Telegram отвергает кусок целиком.
    """
    chunks = split_message(body, limit=3000) or [""]
    for index, chunk in enumerate(chunks):
        head = header_html + "\n\n" if index == 0 else ""
        markup = keyboard if index == len(chunks) - 1 else None
        piece = head + to_html("```\n" + chunk + "\n```")
        try:
            await bot.send_message(chat_id, piece, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log.warning("HTML отвергнут Telegram, шлю без разметки", exc_info=True)
            await bot.send_message(
                chat_id, re.sub(r"<[^>]+>", "", head) + chunk, reply_markup=markup
            )


async def answer_html(message: Message, text: str) -> None:
    """Ответ на сообщение длинным HTML, с разбиением по лимиту Telegram."""
    await send_long(message.bot, message.chat.id, text)


async def send_files(message: Message, files) -> None:
    """Отправляет менеджеру файлы, которые модель запросила через request_file."""
    for entry in files:
        try:
            await message.answer_document(
                FSInputFile(str(entry.binary), filename=entry.filename),
                caption=f"{entry.title}" + (f" (выдан {entry.given})" if entry.given else ""),
            )
            log.info("Отправлен файл %s менеджеру %s", entry.id, message.from_user.id)
        except Exception:
            log.exception("Не смог отправить файл %s", entry.id)
            await message.answer(
                f"Файл «{entry.title}» есть в библиотеке, но отправить не вышло — скажи руководителю."
            )


async def notify_leaders(
    bot: Bot, config, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> int:
    """Рассылает уведомление всем руководителям (по id). Возвращает, скольким дошло."""
    sent = 0
    for admin_id in config.leaders:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
            sent += 1
        except Exception:
            log.warning("Не смог уведомить руководителя %s", admin_id, exc_info=True)
    return sent


async def notify_manager(bot: Bot, manager_id: int, text: str) -> None:
    """Сообщает менеджеру о судьбе его предложения; если не дошло — только лог."""
    try:
        await bot.send_message(manager_id, text)
    except Exception:
        log.warning("Не смог уведомить менеджера %s", manager_id, exc_info=True)


async def clear_markup(callback: CallbackQuery) -> None:
    """Снимает кнопки, не роняя обработчик."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        log.info("Не смог снять кнопки у сообщения — продолжаю", exc_info=True)


async def keep_typing(bot: Bot, chat_id: int) -> None:
    """Ответ занимает 10–30 секунд: держим «печатает», чтобы не выглядело зависшим."""
    try:
        while True:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def cut(text: str, limit: int) -> str:
    """То же, но сначала схлопывает переносы: для однострочных превью в списках."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def trim_dict(d: dict, limit: int = 500) -> None:
    """Не даём вспомогательным словарям расти бесконечно — держим последние N ключей."""
    while len(d) > limit:
        d.pop(next(iter(d)))
