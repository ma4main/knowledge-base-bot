"""Регрессия качества ответов: «вопрос → что в ответе обязано быть».

    docker compose run --rm kb-bot python answer_check.py [файл] [--model=МОДЕЛЬ] [--chat]

На каждом вопросе проверяется: есть ли в ответе ключевой факт, нет ли запрещённого
(устаревшее значение, выдуманная цифра), уложился ли ответ в потолок строк для
своего типа, сколько стоило и заняло. Набор — `test_answers.txt`; прогон ручной,
потому что каждый вопрос — платный запрос к модели.
"""

from __future__ import annotations

import asyncio
import sys
import time

import qtype
from agent import Agent, Dialog
from config import Config
from factcheck import ClientPhraseAudit
from files_lib import FileLibrary
from kb import KnowledgeBase
from links import LinkBook
from llm import OpenRouterClient
from people import PeopleBook
from qtype import Classifier
from tools import ToolRunner


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def load_cases(path: str) -> list[tuple[str, list[str], list[str]]]:
    """Строки «вопрос => обязательные | НЕ: запрещённые». Внутри пункта «/» — синонимы."""
    cases = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            question, _, tail = line.partition("=>")
            wanted_part, _, forbidden_part = tail.partition("| НЕ:")
            wanted = [p.strip() for p in wanted_part.split(",") if p.strip()]
            forbidden = [p.strip() for p in forbidden_part.split(",") if p.strip()]
            cases.append((question.strip(), wanted, forbidden))
    return cases


def missing(answer: str, wanted: list[str]) -> list[str]:
    """Каких обязательных признаков в ответе нет. «/» внутри пункта — синонимы."""
    text = normalize(answer)
    absent = []
    for item in wanted:
        variants = [normalize(v) for v in item.split("/") if v.strip()]
        if not any(v in text for v in variants):
            absent.append(item)
    return absent


def leaked(answer: str, forbidden: list[str]) -> list[str]:
    text = normalize(answer)
    return [item for item in forbidden if normalize(item) in text]


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else "test_answers.txt"
    model_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--model=")), None)
    in_group = "--chat" in sys.argv[1:]

    config = Config.from_env()
    kb = KnowledgeBase(config.kb_root)
    files = FileLibrary(config.kb_root)
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    model = model_arg or config.model
    # Собираем агента ровно как в main.py: прогон должен мерить то, что уходит на проде.
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

    cases = load_cases(path)
    where = "рабочий чат" if in_group else "личка"
    print(f"Прогон ответов: {len(cases)} вопросов, модель {model}, {where}\n")

    hits = 0
    cost = 0.0
    seconds = 0.0
    problems: list[str] = []
    over_limit = 0
    try:
        for question, wanted, forbidden in cases:
            started = time.monotonic()
            try:
                result = await agent.answer(Dialog(), question, model=model, in_group=in_group)
            except Exception as error:  # прогон не должен падать целиком из-за одного вопроса
                problems.append(f"{question[:55]}… — ошибка: {error}")
                print(f"[СБОЙ] {question[:58]}")
                continue
            took = time.monotonic() - started
            seconds += took
            cost += result.usage.cost

            absent = missing(result.text, wanted)
            # kb-id в ответе запрещены всегда — проверяем на каждом вопросе, а не в наборе.
            extra = leaked(result.text, [*forbidden, "kb-"])
            lines = len([ln for ln in result.text.splitlines() if ln.strip()])
            # Потолок — второе число пары «ориентир / потолок» для этого типа.
            _, limit = qtype.LINE_LIMITS.get(
                (result.kind, in_group),
                qtype.LINE_LIMITS[(qtype.OTHER, in_group)],
            )
            too_long = bool(limit) and lines > limit
            over_limit += 1 if too_long else 0
            ok = not absent and not extra
            hits += 1 if ok else 0

            mark = "OK " if ok else "МИМО"
            flags = []
            if absent:
                flags.append("нет: " + ", ".join(absent))
            if extra:
                flags.append("лишнее: " + ", ".join(extra))
            if too_long:
                flags.append(f"длина {lines} строк при потолке {limit}")
            if flags:
                problems.append(f"{question[:55]}… — {'; '.join(flags)}")
            print(
                f"[{mark}] {question[:52]:<52} {qtype.LABELS.get(result.kind, result.kind):<12} "
                f"{lines:>2} стр, {took:>4.1f} сек" + (f"  ⚠ {flags[0]}" if flags else "")
            )
    finally:
        await llm.aclose()

    total = len(cases) or 1
    print(
        f"\nВерных ответов: {hits}/{len(cases)} ({100 * hits / total:.0f}%), "
        f"вышли за формат: {over_limit}"
    )
    print(f"Стоимость прогона ${cost:.4f}, в среднем {seconds / total:.1f} сек на вопрос")
    if problems:
        print("\nЧТО РАЗБИРАТЬ:")
        for item in problems:
            print(f"  • {item}")


if __name__ == "__main__":
    asyncio.run(main())
