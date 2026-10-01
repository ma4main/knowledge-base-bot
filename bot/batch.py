"""Батч-прогон вопросов через бота. Каждый вопрос — свежий диалог.

Запуск на сервере:
    docker compose run --rm kb-bot python batch.py test_questions.txt > /tmp/batch.jsonl
Печатает по строке JSON на вопрос: вопрос, ответ, найденные kb-id и файлы, расход.
Дальше ответы оценивает отдельный прогон против базы.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from agent import Agent, Dialog
from config import Config
from files_lib import FileLibrary
from kb import KB_ID_RE, KnowledgeBase
from links import LinkBook
from llm import OpenRouterClient
from tools import ToolRunner


def load_questions(path: str) -> list[str]:
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "test_questions.txt"
    config = Config.from_env()
    kb = KnowledgeBase(config.kb_root)
    files = FileLibrary(config.kb_root)
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    # Агент собирается как в main.py: иначе прогон оценивает не то, что видят менеджеры.
    agent = Agent(
        llm, ToolRunner(kb, files), kb.index_text, config.cache_ttl,
        config.max_tool_iterations, kb=kb, links=LinkBook(config.kb_root),
    )

    questions = load_questions(path)
    print(f"# модель {config.model}, вопросов {len(questions)}", file=sys.stderr)

    try:
        for i, q in enumerate(questions, 1):
            started = time.monotonic()
            try:
                result = await agent.answer(Dialog(), q, model=config.model)
                answer, usage, sent, error = result.text, result.usage, result.files, None
            except Exception as exc:
                answer, usage, sent, error = "", None, [], str(exc)
            row = {
                "n": i,
                "question": q,
                "answer": answer,
                "kb_ids": sorted(set(KB_ID_RE.findall(answer))),
                "files_sent": [e.id for e in sent],
                "seconds": round(time.monotonic() - started, 1),
                "cost": round(usage.cost, 5) if usage else None,
                "cached_pct": (
                    round(100 * usage.cached_tokens / usage.prompt_tokens)
                    if usage and usage.prompt_tokens
                    else None
                ),
                "error": error,
            }
            print(json.dumps(row, ensure_ascii=False), flush=True)
            print(f"# {i}/{len(questions)} готов ({row['seconds']}с)", file=sys.stderr)
    finally:
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
