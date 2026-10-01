"""Прогон пути записи на настоящей модели. В базу НЕ пишет: репозиторий смонтирован
только на чтение, а commit() здесь не зовётся вовсе.

Показывает ровно то, что увидит человек:
1) менеджер в личке: «запомни …» → сообщение с формулировкой и кнопками;
2) автор в рабочем чате: черновик с кнопками «Верно / Не надо».
"""
import asyncio
import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app/bot")
# Запасной путь — для запуска вне контейнера, из корня репозитория.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "bot"))

from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message

import h_edit
from config import Config
from kb import KnowledgeBase
from kb_editor import KbEditor
from llm import OpenRouterClient
from state import State

SENT = []
NOW = datetime.datetime.now(datetime.timezone.utc)


class FakeBot(Bot):
    async def __call__(self, method, request_timeout=None):
        if isinstance(method, SendMessage):
            SENT.append(method)
            return Message(message_id=len(SENT), date=NOW,
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True


def show(title):
    print(f"\n===== {title} =====")
    for m in SENT:
        print(m.text)
        if m.reply_markup is not None and hasattr(m.reply_markup, "inline_keyboard"):
            print("   КНОПКИ:", [b.text for row in m.reply_markup.inline_keyboard for b in row])
        print("-----")
    SENT.clear()


async def main():
    fact = " ".join(sys.argv[1:]) or "запомни: срез позиций по Трафику теперь до 15:00, а не до 14:00, с 1 октября"
    config = Config.from_env()
    tmp = Path(tempfile.mkdtemp())
    kb = KnowledgeBase(config.kb_root)
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    editor = KbEditor(llm, kb, config.model)
    state = State(tmp / "state.json", default_model=config.model)
    bot = FakeBot(token="123456:TEST")
    try:
        # 1. Личка, менеджер (leader=False).
        await h_edit.run_update(bot, 20, 20, state, editor, fact, author="Пётр Ильин", leader=False)
        show("ЛИЧКА, МЕНЕДЖЕР: выбор темы")
        if h_edit.PENDING_CREATE_ASK:
            ask_id = list(h_edit.PENDING_CREATE_ASK)[-1]
            await h_edit._prepare_edit(bot, 20, ask_id, state, editor, "kb-402")
            show("ЛИЧКА, МЕНЕДЖЕР: формулировка")
        pending = list(h_edit.PENDING_EDITS.values())
        if pending:
            new_text = pending[-1][1].new_text
            tail = [ln for ln in new_text.splitlines() if "внёс Пётр Ильин" in ln or "<!-- claim" in ln]
            print("ЧТО ЛЯЖЕТ В ФАЙЛ (строки с подписью и провенансом):")
            for ln in tail:
                print("  ", ln)

    finally:
        await llm.aclose()
        await bot.session.close()


asyncio.run(main())
