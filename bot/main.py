"""Точка входа. Long polling: порт не занимается."""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher

from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

import access
import alerts
import handlers
import heartbeat
import ui
from agent import Agent
from answer_cache import AnswerCache
from autonomy import AutoWriter
import chat_log as chat_log_module
from chat_log import ChatLog
from daily import DailyDigest
from dialogs import DialogStore
from config import Config
from factcheck import ClientPhraseAudit
from files_lib import FileLibrary
from kb import KnowledgeBase
from kb_editor import KbEditor
from links import LinkBook
from llm import OpenRouterClient
from notes import FeedbackLog, SuggestionLog
from people import PeopleBook
import publisher as publisher_module
from publisher import Publisher
from qtype import Classifier
from state import State
from tools import ToolRunner
from usage import Transcript, UsageLog

log = logging.getLogger("bot")


async def run() -> None:
    config = Config.from_env()
    # Лог идёт и в stdout, и в файл в томе данных: docker-логи живут только
    # до пересоздания контейнера.
    file_log = RotatingFileHandler(
        config.data_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), file_log],
    )

    knowledge = KnowledgeBase(config.kb_root)
    files = FileLibrary(config.kb_root)
    links = LinkBook(config.kb_root)
    # Пустой том с данными (новый сервер): поднимаем состояние из снимка в репозитории.
    publisher_module.restore_state(config.kb_root, config.data_dir / "state.json")
    app_state = State(config.data_dir / "state.json", default_model=config.model)
    # Руководители живут в состоянии бота; .env — только начальная загрузка.
    access.sync_leaders(config, app_state)
    if not config.leaders:
        log.warning(
            "Нет ни одного руководителя: выдавать доступы некому. Впиши Telegram id "
            "в LEADER_IDS в .env и перезапусти — он импортируется в состояние бота."
        )
    if not (config.has_whitelist or app_state.extra_managers):
        log.warning("Доступа нет ни у кого — бот откажет всем. Первого руководителя — в LEADER_IDS в .env.")
    # Справочник людей: подтверждённая часть — knowledge/PEOPLE.md, наблюдённые
    # подписи «@ник → имя» — из состояния бота.
    people = PeopleBook(config.kb_root, app_state)
    publisher = Publisher(config.kb_root, app_state)
    answer_cache = AnswerCache(
        config.data_dir / "answer_cache.json", unit_hashes=knowledge.unit_hashes()
    )
    # Поток чатов лежит в репозитории отдельным слоем `chats-live/` и уезжает в git пачкой.
    chat_log = ChatLog(config.kb_root / chat_log_module.REL_DIR)
    # Разговоры на диске: переживают деплой, забываются через 12 часов молчания.
    dialog_store = DialogStore(config.data_dir / "dialogs.json")
    usage_log = UsageLog(config.data_dir / "usage.jsonl")
    transcript = Transcript(config.data_dir / "transcripts.jsonl")
    feedback_log = FeedbackLog(config.data_dir / "feedback.jsonl")
    suggestion_log = SuggestionLog(config.data_dir / "suggestions.jsonl")
    llm = OpenRouterClient(
        api_key=config.openrouter_key,
        model=config.model,
        fallbacks=config.model_fallbacks,
        timeout=config.request_timeout,
    )
    agent = Agent(
        llm=llm,
        tools=ToolRunner(knowledge, files),
        index_text=knowledge.index_text,
        cache_ttl=config.cache_ttl,
        max_iterations=config.max_tool_iterations,
        # Ссылки и каталог агент читает живыми: бот правит базу на ходу.
        kb=knowledge,
        links=links,
        people=people,
        # Запасная модель классификатора — основная.
        classifier=Classifier(llm, config.classifier_model, fallback_model=config.model),
        # Сверка фразы для клиента с источником — той же дешёвой моделью.
        audit=(
            ClientPhraseAudit(llm, config.classifier_model)
            if config.audit_client_phrases else None
        ),
    )
    # on_usage — расход редактора попадает в тот же журнал, что и расход ответов;
    # user_id = 0 — «не человек, а сам бот».
    kb_editor = KbEditor(
        llm, knowledge, config.model, cache_ttl=config.cache_ttl,
        on_usage=lambda model, usage: usage_log.record(0, model, usage, 0.0),
    )
    # Конвейер автономного пополнения; сам флаг — в состоянии.
    auto_writer = AutoWriter(
        knowledge, kb_editor, publisher, answer_cache, app_state, handlers.KB_WRITE_LOCK
    )

    bot = Bot(token=config.telegram_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(handlers.router)
    # Прокидываем зависимости в обработчики, чтобы не заводить глобалы.
    dispatcher["config"] = config
    dispatcher["agent"] = agent
    dispatcher["app_state"] = app_state
    dispatcher["usage_log"] = usage_log
    dispatcher["transcript"] = transcript
    dispatcher["files"] = files
    dispatcher["publisher"] = publisher
    dispatcher["feedback_log"] = feedback_log
    dispatcher["suggestion_log"] = suggestion_log
    dispatcher["kb"] = knowledge
    dispatcher["kb_editor"] = kb_editor
    dispatcher["links"] = links
    dispatcher["answer_cache"] = answer_cache
    dispatcher["chat_log"] = chat_log
    dispatcher["dialog_store"] = dialog_store
    dispatcher["auto_writer"] = auto_writer

    me = await bot.get_me()
    log.info(
        "Запущен @%s | модель %s | классификатор %s | менеджеров в .env: %d id + %d ников, "
        "добавлено из бота: %d, руководителей: %d",
        me.username,
        app_state.model,
        config.classifier_model,
        len(config.managers),
        len(config.manager_usernames),
        len(app_state.extra_managers),
        len(config.leaders),
    )

    # Короткий список команд у строки ввода; само меню — кнопками (h_menu.py).
    await _setup_commands(bot, config)

    # Фоновая публикация сырья в git.
    publish_task = asyncio.create_task(publisher.run_periodic())
    # Сводка по рабочим чатам.
    digest = DailyDigest(
        bot, config, app_state, chat_log, kb_editor, ui.notify_leaders,
        # kb — для напоминаний о гипотезах с прошедшей контрольной точкой.
        kb=knowledge,
        # auto — что бот внёс сам, попадает в ту же сводку с кнопкой «Откатить».
        auto=auto_writer,
    )
    digest_task = asyncio.create_task(digest.run_periodic())

    # Отметка живости: пишется после успешного обращения к Telegram (heartbeat.py, HEALTHCHECK).
    heartbeat_task = asyncio.create_task(heartbeat.run(bot, config.data_dir))

    # Критичные сигналы руководителям (alerts.py).
    alerts_task = asyncio.create_task(alerts.run(bot, config, app_state, ui.notify_leaders))

    # Сторож файлов базы: замечает правки, сделанные НЕ через бота.
    watch_task = asyncio.create_task(_watch_knowledge(knowledge, answer_cache))

    try:
        await dispatcher.start_polling(bot)
    finally:
        watch_task.cancel()
        alerts_task.cancel()
        heartbeat_task.cancel()
        digest_task.cancel()
        publish_task.cancel()
        await llm.aclose()
        await bot.session.close()


WATCH_EVERY = 60  # раз в минуту сверяем отпечаток файлов базы


async def _watch_knowledge(knowledge: KnowledgeBase, answer_cache: AnswerCache) -> None:
    """Перечитывает базу, если файлы изменились мимо бота (git pull, правка руками);
    отпечаток дешёвый (число файлов и последняя mtime), поэтому раз в минуту."""
    known = knowledge.disk_fingerprint()
    while True:
        await asyncio.sleep(WATCH_EVERY)
        try:
            current = knowledge.disk_fingerprint()
            if current == known:
                continue
            # Под тем же локом, что и запись через бота: иначе перечитывание может
            # попасть между записью файла и коммитом.
            async with handlers.KB_WRITE_LOCK:
                known = knowledge.disk_fingerprint()
                knowledge.reload()
                answer_cache.sync(knowledge.unit_hashes())
            log.info("Файлы базы изменились мимо бота — перечитал: %d единиц", len(knowledge.units))
        except Exception:
            log.exception("Сторож базы споткнулся — попробую на следующем круге")


async def _setup_commands(bot: Bot, config: Config) -> None:
    """Короткий список команд у строки ввода, один на всех. Списки руководителей
    (scope по chat_id) Telegram хранит у себя, поэтому их снимаем явно."""
    base = [
        BotCommand(command="menu", description="Меню"),
        BotCommand(command="reset", description="Начать разговор заново"),
        BotCommand(command="whoami", description="Мой Telegram ID и роль"),
    ]
    try:
        await bot.set_my_commands(base, scope=BotCommandScopeDefault())
        for leader_id in config.leaders | set(config.env_leaders):
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=leader_id))
    except Exception:
        log.warning("Не смог установить меню команд", exc_info=True)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Остановлен вручную")
    except RuntimeError as error:
        # Понятное сообщение вместо трейсбэка при кривом конфиге.
        log.error("%s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
