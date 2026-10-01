"""Регрессия классификатора запросов: измеряет, как часто тип определён верно.

    docker compose run --rm kb-bot python type_check.py [файл] [--model=МОДЕЛЬ]

Формат ответа выбирается по типу запроса, поэтому ошибка классификатора портит
весь ответ, а в обычных проверках не видна — здесь она становится числом. Набор
случаев — `test_types.txt`, пополнять вопросами из `transcripts.jsonl` (поле `kind`).
Ничего не пишет и базу не читает: только классификация.
"""

from __future__ import annotations

import asyncio
import sys

import qtype
from config import Config
from llm import OpenRouterClient


def load_cases(path: str) -> list[tuple[str, list[str]]]:
    """Строки «вопрос => ТИП» или «вопрос => ТИП, ТИП» (любой из них — попадание)."""
    cases: list[tuple[str, list[str]]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            question, _, expected = line.partition("=>")
            wanted = []
            for chunk in expected.split(","):
                label = chunk.strip().upper()
                if not label:
                    continue
                kind = qtype.BY_LABEL.get(label)
                if kind is None:
                    raise SystemExit(
                        f"В {path} неизвестный тип «{label}». "
                        f"Допустимые: {', '.join(qtype.LABELS.values())}"
                    )
                wanted.append(kind)
            if wanted:
                cases.append((question.strip(), wanted))
    return cases


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else "test_types.txt"
    model_arg = next(
        (a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--model=")), None
    )

    config = Config.from_env()
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    model = model_arg or config.classifier_model
    # Без запасной модели: меряем именно указанную, а не смесь двух.
    classifier = qtype.Classifier(llm, model)

    cases = load_cases(path)
    print(f"Прогон классификатора: {len(cases)} случаев, модель {model}\n")

    hits = 0
    lenient = 0
    misses: list[str] = []
    cost = 0.0
    try:
        for question, wanted in cases:
            kind, usage = await classifier.classify(question)
            cost += usage.cost
            ok = kind in wanted
            mark = "OK " if ok else "МИМО"
            if ok and len(wanted) > 1:
                mark = "OK?"
                lenient += 1
            hits += 1 if ok else 0
            if not ok:
                misses.append(
                    f"{question[:60]}… ждали {'/'.join(qtype.LABELS[w] for w in wanted)}, "
                    f"получили {qtype.LABELS[kind]}"
                )
            print(
                f"[{mark}] {question[:58]:<58} "
                f"ждали {'/'.join(qtype.LABELS[w] for w in wanted):<20} "
                f"→ {qtype.LABELS[kind]}"
            )
    finally:
        await llm.aclose()

    total = len(cases)
    print(f"\nПопаданий: {hits}/{total} ({100 * hits / total:.0f}%), стоимость ${cost:.5f}")
    if lenient:
        print(f"Из них спорных случаев (допускалось несколько типов): {lenient}")
    if misses:
        print("\nПРОМАХИ — их и надо разбирать:")
        for miss in misses:
            print(f"  • {miss}")


if __name__ == "__main__":
    asyncio.run(main())
