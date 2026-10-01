"""Живая проверка всей цепочки: один реальный вопрос через OpenRouter.

Не входит в обычный selfcheck — требует ключа и сети. Запуск на сервере:
    docker compose run --rm kb-bot python smoke.py
Печатает ответ модели и расход.
"""

from __future__ import annotations

import asyncio
import time

from agent import Agent, Dialog
from config import Config
from factcheck import ClientPhraseAudit
from files_lib import FileLibrary
from kb import KnowledgeBase
from links import LinkBook
from llm import OpenRouterClient
from people import PeopleBook
from qtype import Classifier, LABELS
from safety import as_data
from tools import ToolRunner


async def main() -> None:
    config = Config.from_env()
    kb = KnowledgeBase(config.kb_root)
    files = FileLibrary(config.kb_root)
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    # Агент собирается как в main.py: иначе прогон измеряет не то, что уходит в модель.
    agent = Agent(
        llm, ToolRunner(kb, files), kb.index_text, config.cache_ttl,
        config.max_tool_iterations, kb=kb, links=LinkBook(config.kb_root),
        classifier=Classifier(llm, config.classifier_model, fallback_model=config.model),
        people=PeopleBook(config.kb_root),
        audit=(
            ClientPhraseAudit(llm, config.classifier_model)
            if config.audit_client_phrases else None
        ),
    )

    import sys
    flags = {"--chat", "--more"}
    raw = [a for a in sys.argv[1:] if a not in flags and not a.startswith("--ctx=")]
    # --chat: тот же вопрос в формате рабочего чата.
    # --more: следом нажать «Подробнее» — проверить второй, развёрнутый ответ.
    # --ctx=«…|…»: подложить переписку топика (сообщения через |).
    in_group = "--chat" in sys.argv[1:]
    with_more = "--more" in sys.argv[1:]
    ctx_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--ctx=")), "")
    context = (
        as_data("\n".join(ctx_arg.split("|")), "ПОСЛЕДНИЕ СООБЩЕНИЯ В ЭТОМ ТОПИКЕ")
        if ctx_arg else ""
    )
    args = raw
    question = " ".join(args) or "у клиента резко просели показатели, что делать?"
    print(f"Модель: {config.model} | классификатор: {config.classifier_model}")
    print(f"Вопрос: {question}")
    print(f"Место: {'рабочий чат' if in_group else 'личка'}")
    if context:
        print(f"Контекст топика: {len(ctx_arg.split('|'))} сообщений")
    print()

    started = time.monotonic()
    detailed = None
    try:
        result = await agent.answer(
            Dialog(), question, model=config.model, in_group=in_group, context=context
        )
        if with_more:
            detailed = await agent.expand(
                question, result.units, model=config.model, in_group=in_group
            )
    finally:
        await llm.aclose()

    print(f"=== ОТВЕТ (тип: {LABELS.get(result.kind, result.kind)}) ===")
    print(result.text)
    print(f"\nстрок: {len(result.text.strip().splitlines())}")
    print(f"единицы: {', '.join(result.units) or 'нет'}")
    if result.files:
        print("\n=== ФАЙЛЫ К ОТПРАВКЕ ===")
        for entry in result.files:
            print(f"- {entry.id}: {entry.filename}")
    print("\n=== РАСХОД ===")
    print(result.usage)
    if detailed is not None:
        print("\n=== ПОДРОБНЕЕ (второй ответ по тем же единицам) ===")
        print(detailed.text)
        print(f"\nстрок: {len(detailed.text.strip().splitlines())}")
        print(f"расход: {detailed.usage}")
    print(f"время: {time.monotonic() - started:.1f} сек")


if __name__ == "__main__":
    asyncio.run(main())
