"""Прогон меню и управления людьми через настоящий Dispatcher с подставным ботом.

Запуск в контейнере: python docs/scripts/menu_e2e.py (база в /app/kb);
локально: KB_ROOT=. python docs/scripts/menu_e2e.py из корня репозитория.
Сеть не нужна: все вызовы Telegram перехватываются и записываются.
"""
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app/bot")
# Запасной путь — для запуска вне контейнера, из корня репозитория.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "bot"))

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import access
import handlers
from config import Config
from dialogs import DialogStore
from files_lib import FileLibrary
from kb import KnowledgeBase
from links import LinkBook
from state import State

CALLS = []
NOW = datetime.datetime.now(datetime.timezone.utc)
_next_id = [100]


class FakeBot(Bot):
    async def __call__(self, method, request_timeout=None):
        CALLS.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            _next_id[0] += 1
            return Message(
                message_id=_next_id[0], date=NOW,
                chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"),
                text=method.text,
            )
        return True


def texts():
    out = []
    for m in CALLS:
        if isinstance(m, (SendMessage, EditMessageText)):
            out.append(("send" if isinstance(m, SendMessage) else "edit", m.chat_id, m.text))
        elif isinstance(m, AnswerCallbackQuery):
            out.append(("alert" if m.show_alert else "ack", None, m.text or ""))
    return out


def buttons():
    for m in reversed(CALLS):
        markup = getattr(m, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    return []


async def main():
    tmp = Path(tempfile.mkdtemp())
    kb_root = Path(os.environ.get("KB_ROOT") or "/app/kb")
    config = Config.__new__(Config)
    for key, value in {
        "managers": set(), "leaders": set(), "env_leaders": frozenset({10}),
        "manager_usernames": set(), "kb_root": kb_root, "data_dir": tmp,
    }.items():
        object.__setattr__(config, key, value)
    state = State(tmp / "state.json", default_model="m")
    access.sync_leaders(config, state)
    state.add_manager(10, user_id=20, username="demo_p_ilyin", name="Пётр Ильин")

    bot = FakeBot(token="123456:TEST")
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp["config"] = config
    dp["app_state"] = state
    dp["kb"] = KnowledgeBase(kb_root)
    dp["links"] = LinkBook(kb_root)
    dp["files"] = FileLibrary(kb_root)
    dp["dialog_store"] = DialogStore(tmp / "dialogs.json")
    for stub in ("agent", "usage_log", "transcript", "suggestion_log", "kb_editor",
                 "publisher", "answer_cache", "auto_writer", "feedback_log", "chat_log"):
        dp[stub] = None

    leader = User(id=10, is_bot=False, first_name="Анна")
    manager = User(id=20, is_bot=False, first_name="Пётр", username="demo_p_ilyin")
    uid = [0]

    async def say(user, text):
        uid[0] += 1
        CALLS.clear()
        msg = Message(message_id=uid[0], date=NOW, chat=Chat(id=user.id, type="private"),
                      from_user=user, text=text)
        await dp.feed_update(bot, Update(update_id=uid[0], message=msg))

    async def press(user, data):
        uid[0] += 1
        CALLS.clear()
        menu_msg = Message(message_id=999, date=NOW, chat=Chat(id=user.id, type="private"),
                           from_user=User(id=1, is_bot=True, first_name="bot"), text="меню")
        cb = CallbackQuery(id=str(uid[0]), from_user=user, chat_instance="x",
                           message=menu_msg, data=data)
        await dp.feed_update(bot, Update(update_id=uid[0], callback_query=cb))

    failures = []

    def check(cond, note):
        print(("ok   " if cond else "FAIL ") + note)
        if not cond:
            failures.append(note)

    await say(leader, "/menu")
    check("m:do:people" in buttons() and "m:nav:manage" in buttons(), "руководитель: в меню есть люди и управление")
    await say(manager, "☰ Меню")
    check("m:do:update" in buttons() and "m:do:people" not in buttons(), "менеджер: запись есть, управления людьми нет")

    await press(manager, "m:do:people")
    check(any(k == "alert" for k, _, _ in texts()), "менеджер жмёт чужой пункт → отказ")
    await press(manager, "m:nav:manage")
    check(any(k == "alert" for k, _, _ in texts()), "менеджер идёт на экран руководителя → отказ")
    await press(manager, "m:ppl:addl2:20")
    check(20 not in state.leader_ids(), "менеджер не может назначить себя руководителем")

    await press(manager, "m:nav:base")
    check(any(k == "edit" for k, _, _ in texts()) and "m:do:journal" in buttons(), "экран «База» открывается в том же сообщении")
    await press(manager, "m:do:links")
    check(any(k == "send" and c == 20 for k, c, _ in texts()), "пункт «Ссылки» зовёт обработчик /links и отвечает нажавшему")
    await press(manager, "m:do:hypotheses")
    check(any(k == "send" for k, _, _ in texts()), "пункт «Гипотезы» отвечает менеджеру")
    await press(manager, "m:do:whoami")
    check(any("менеджер" in (t or "") for _, _, t in texts()), "«Мой ID и роль» называет роль словом")
    await press(manager, "m:do:update")
    from botstate import PENDING_UPDATE
    check(20 in PENDING_UPDATE, "«Внести в базу» включает режим ввода у менеджера")
    await say(manager, "отмена")
    check(20 not in PENDING_UPDATE, "«отмена» снимает режим ввода")

    await press(leader, "m:do:people")
    check(any("Руководители — 1" in (t or "") for _, _, t in texts()), "экран людей показывает руководителей")
    await press(leader, "m:ppl:rml")
    check(any("Руководитель один" in (t or "") for _, _, t in texts()), "единственного руководителя снять нельзя")
    await press(leader, "m:ppl:addl")
    check("m:ppl:addl1:20" in buttons(), "в кандидаты в руководители попал менеджер с известным id")
    await press(leader, "m:ppl:addl1:20")
    check("m:ppl:addl2:20" in buttons(), "перед назначением спрашивается подтверждение")
    await press(leader, "m:ppl:addl2:20")
    check(config.role(20) == "leader", "назначение сработало без перезапуска")
    check(any(isinstance(m, SendMessage) and m.chat_id == 20 for m in CALLS), "новому руководителю пришло уведомление")

    await say(manager, "/menu")
    check("m:do:people" in buttons(), "у нового руководителя в меню появилось управление")
    await press(manager, "m:ppl:rml2:10")
    check(access.resolve_role(config, state, 10, None) == "manager" and state.leader_ids() == {20},
          "передача: новый руководитель снял прежнего, тот остался менеджером")
    await press(leader, "m:do:people")
    check(any(k == "alert" for k, _, _ in texts()), "снятый руководитель по старой кнопке в управление не попадает")

    await press(manager, "m:ppl:addm")
    await say(manager, "@new_person")
    check(state.is_extra_manager(0, "new_person"), "менеджер добавлен по нику через диалог")
    await press(manager, "m:ppl:addm")
    await say(manager, "/menu")
    from botstate import PENDING_MENU_INPUT
    check(20 not in PENDING_MENU_INPUT and not state.is_extra_manager(0, "menu"),
          "команда вместо ника снимает режим ввода, а не записывается как ник")
    await press(manager, "m:ppl:rmm2:new_person")
    check(not state.is_extra_manager(0, "new_person"), "доступ менеджера забирается кнопкой")

    # --- Файл от менеджера: сразу в библиотеку, со сноской «кто и когда» ---
    import uploads
    from botstate import PENDING_UPLOADS
    lib_root = tmp / "lib"
    (lib_root / "files" / ".staging").mkdir(parents=True)
    staged = lib_root / "files" / ".staging" / "x.pdf"
    staged.write_bytes(b"%PDF-1.4")
    lib = FileLibrary(lib_root)
    dp["files"] = lib
    outsider = User(id=30, is_bot=False, first_name="Ольга", username="demo_o_zaytseva")
    state.add_manager(20, user_id=30, username="demo_o_zaytseva", name="Ольга")
    PENDING_UPLOADS[30] = uploads.PendingUpload(
        staged=staged, original_name="x.pdf", ext=".pdf", added_by="Ольга", simple=True,
    )
    await say(outsider, "Прайс по Трафику для менеджеров\nактуален с сентября")
    cards = list((lib_root / "files").glob("*.md"))
    card = cards[0].read_text(encoding="utf-8") if cards else ""
    check(bool(cards) and "Добавил: Ольга, " in card and "title: Прайс по Трафику для менеджеров" in card,
          "файл менеджера лёг в библиотеку сразу, в карточке есть «Добавил: Имя, дата»")
    check(any("файл в библиотеке" in (t or "") and "Добавил: Ольга" in (t or "") for _, _, t in texts()),
          "менеджеру пришло подтверждение со сноской, без согласования с руководителем")
    check(not any(isinstance(m, SendMessage) and m.chat_id in (10,) for m in CALLS),
          "руководителю про файл менеджера ничего не ушло")
    await press(outsider, "m:do:files")
    check(any("Добавил: Ольга" in (t or "") for _, _, t in texts()), "в списке файлов видна сноска, кто добавил")

    # --- Новый чат: до решения руководителя не записывается ---
    dp["chat_log"] = type("FakeChatLog", (), {"counts": lambda self: {}})()
    uid[0] += 1
    CALLS.clear()
    group_msg = Message(
        message_id=uid[0], date=NOW, chat=Chat(id=-100500, type="supergroup", title="Клиент & Маяк"),
        from_user=outsider, text="добрый день, когда будет отчёт?",
    )
    await dp.feed_update(bot, Update(update_id=uid[0], message=group_msg))
    check(not state.is_listening(-100500), "новый чат до решения руководителя не записывается")
    asked = [m for m in CALLS if isinstance(m, SendMessage) and m.chat_id == 20]
    check(bool(asked) and "lst:hear:-100500" in buttons() and "lst:mute:-100500" in buttons(),
          "руководителю пришёл вопрос про новый чат с кнопками «Рабочий / Клиентский»")
    await press(manager, "lst:hear:-100500")
    check(state.is_listening(-100500), "после «Рабочий» чат начинает записываться")
    await press(manager, "lst:mute:-100500")
    check(not state.is_listening(-100500), "после «Клиентский» запись выключается")

    await bot.session.close()
    print("\nИТОГ:", "всё прошло" if not failures else f"провалов {len(failures)}: {failures}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
