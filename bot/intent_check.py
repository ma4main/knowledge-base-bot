"""Регрессия распознавания намерения: «обращение => отвечать или записывать».

    docker compose run --rm kb-bot python intent_check.py [файл]

Зачем отдельно от `type_check.py`. Тот меряет ФОРМАТ ответа, и его ошибка стоит
многословия. Здесь меряется решение «ответить или записать в базу», и ошибка
в сторону записи попадает в базу — там её увидят все. Поэтому набор перекошен
в пользу вопросов, и в отчёте они считаются отдельно: **ни один вопрос не должен
уехать в запись**, это главная цифра прогона.

Стоит денег (вызов модели на каждую строку), поэтому прогон ручной, а не в selfcheck.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from config import Config
from llm import OpenRouterClient
from qtype import ASK, INTENT_BY_LABEL, INTENT_LABELS, Classifier

DEFAULT_FILE = Path(__file__).resolve().parent / "test_intents.txt"


def load(path: Path) -> list[tuple[str, str]]:
    cases = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        text, _, label = line.partition("=>")
        intent = INTENT_BY_LABEL.get(label.strip().upper())
        if intent is None:
            print(f"ПРОПУСК (непонятное намерение): {line}")
            continue
        cases.append((text.strip(), intent))
    return cases


async def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FILE
    cases = load(path)
    config = Config.from_env()
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    classifier = Classifier(llm, config.classifier_model, fallback_model=config.model)

    print(f"Прогон намерений: {len(cases)} обращений, модель {config.classifier_model}\n")
    hits = 0
    cost = 0.0
    leaked = []  # вопросы, уехавшие в запись — самое дорогое
    misses = []
    for text, expected in cases:
        got, usage = await classifier.intent(text)
        cost += usage.cost
        if got == expected:
            hits += 1
            continue
        misses.append((text, expected, got))
        if expected == ASK:
            leaked.append((text, got))
        print(f"[МИМО] ждали {INTENT_LABELS[expected]:<9} получили "
              f"{INTENT_LABELS[got]:<9} | {text[:70]}")

    total = len(cases) or 1
    print(f"\nПопаданий: {hits}/{len(cases)} ({round(hits * 100 / total)}%), "
          f"стоимость ${cost:.4f}")
    if leaked:
        print(f"\n🔴 ВОПРОСЫ, УЕХАВШИЕ В ЗАПИСЬ: {len(leaked)} — это и есть цена ошибки:")
        for text, got in leaked:
            print(f"  • {text[:80]} → {INTENT_LABELS[got]}")
    else:
        print("\n✅ Ни один вопрос не уехал в запись.")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
