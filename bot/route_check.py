"""Регрессия маршрутизации правок: измеряет, как часто бот выбирает верную единицу.

    docker compose run --rm kb-bot python route_check.py [файл] [--model=МОДЕЛЬ]

Самая дорогая ошибка редактора — положить апдейт не в ту единицу; бот при этом
«работает». Здесь ошибка становится числом. Ничего не пишет в базу: только
маршрутизация (выбор кандидатов + скептик).
"""

from __future__ import annotations

import asyncio
import sys

from config import Config
from kb import KnowledgeBase
from kb_editor import NO_FITTING_UNIT, KbEditor
from llm import OpenRouterClient

NEW_UNIT = "НОВАЯ"


def load_cases(path: str) -> list[tuple[str, list[str]]]:
    cases: list[tuple[str, list[str]]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            update, _, expected = line.partition("=>")
            wanted = [x.strip() for x in expected.split(",") if x.strip()]
            cases.append((update.strip(), wanted))
    return cases


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else "test_updates.txt"
    model_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--model=")), None)

    config = Config.from_env()
    kb = KnowledgeBase(config.kb_root)
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    model = model_arg or config.model
    editor = KbEditor(llm, kb, model, cache_ttl=config.cache_ttl)

    cases = load_cases(path)
    print(f"Прогон маршрутизации: {len(cases)} случаев, модель {model}\n")

    hits = 0
    ambiguous = 0
    misses: list[str] = []
    try:
        for update, wanted in cases:
            candidates, note = await editor.pick_units(update, model=model)
            if not candidates:
                got = [NEW_UNIT] if note == NO_FITTING_UNIT else [f"ОШИБКА({note[:40]})"]
            else:
                got = [c.kb_id for c in candidates]

            ok = any(g in wanted for g in got)
            # «Спросил, хотя ответ однозначен» — не ошибка, но и не идеал: помечаем.
            noisy = ok and len(got) > 1 and len(wanted) == 1
            mark = "OK " if ok else "МИМО"
            if noisy:
                mark = "OK?"
                ambiguous += 1
            hits += 1 if ok else 0
            if not ok:
                misses.append(f"{update[:60]}… ждали {wanted}, получили {got}")
            print(f"[{mark}] {update[:58]:<58} ждали {','.join(wanted):<22} → {','.join(got)}")
    finally:
        await llm.aclose()

    total = len(cases)
    print(f"\nПопаданий: {hits}/{total} ({100 * hits / total:.0f}%)")
    if ambiguous:
        print(f"Из них со лишними кандидатами (бот переспросит): {ambiguous}")
    if misses:
        print("\nПРОМАХИ — их и надо разбирать:")
        for miss in misses:
            print(f"  • {miss}")


if __name__ == "__main__":
    asyncio.run(main())
