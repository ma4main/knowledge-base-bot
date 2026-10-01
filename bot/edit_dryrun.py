"""Сухой прогон правки базы: готовит diff, НЕ применяет. Для проверки редактора.

    docker compose run --rm kb-bot python edit_dryrun.py "минимальный срок договора теперь три месяца"

Если под апдейт не находится существующая единица — так же сухо прогоняет создание
новой (тот же путь, что в боте после кнопки «Создать новую»).
"""

from __future__ import annotations

import asyncio
import difflib
import sys

from config import Config
from kb import KnowledgeBase
from kb_editor import NO_FITTING_UNIT, KbEditor
from llm import OpenRouterClient


async def main() -> None:
    instruction = " ".join(sys.argv[1:]) or "запомни: минимальный срок договора теперь 3 месяца"
    config = Config.from_env()
    kb = KnowledgeBase(config.kb_root)
    llm = OpenRouterClient(config.openrouter_key, config.model, config.model_fallbacks)
    editor = KbEditor(llm, kb, config.model)
    try:
        candidates, note = await editor.pick_units(instruction)
        if not candidates:
            if note == NO_FITTING_UNIT:
                print("Подходящей единицы нет — прогоняю СОЗДАНИЕ новой.\n")
                await _dry_new(editor, instruction)
                return
            print("НЕ ПРЕДЛОЖЕНО:", note)
            return
        print("Кандидаты, прошедшие скептика:")
        for candidate in candidates:
            print(f"  {candidate.kb_id} — {candidate.title} ({candidate.reason})")
        if len(candidates) > 1:
            print("(в боте здесь выбирает человек; для прогона беру первого)\n")
        proposal, note = await editor.prepare(candidates[0].kb_id, instruction)
    finally:
        await llm.aclose()

    if proposal is None:
        print("НЕ ПРЕДЛОЖЕНО:", note)
        return
    print(f"Единица: {proposal.kb_id}  ({proposal.rel_path})")
    print(f"Суть: {proposal.summary}")
    print(f"Почему сюда: {candidates[0].reason or '(причина не названа)'}")
    print(f"Резкое укорачивание: {proposal.shrinks_a_lot()}")
    print(f"Задето мест: {proposal.changed_regions()}")
    changes, extra = proposal.change_lines()
    print("ЧТО МЕНЯЕТСЯ:" if changes else "ЧТО МЕНЯЕТСЯ: (нечего показать)")
    for item in changes:
        print(f"  • {item}")
    if extra:
        print(f"  … и ещё {extra} мест")
    if proposal.reverted:
        print(f"ПЕРЕПРОВЕРКА ОТКАТИЛА ЛИШНЕЕ ({len(proposal.reverted)}):")
        for item in proposal.reverted:
            print(f"  — {item}")
    else:
        print("Перепроверка: посторонних изменений не нашла")
    print("\n=== DIFF ===")
    print(proposal.diff(max_lines=80))


async def _dry_new(editor: KbEditor, instruction: str) -> None:
    """Сухой прогон создания новой единицы: печатает файл целиком и правку INDEX."""
    proposal, note = await editor.propose_new(instruction, author="руководителя (сухой прогон)")
    if proposal is None:
        print("НЕ ПРЕДЛОЖЕНО:", note)
        return
    print(f"Новая единица: {proposal.kb_id} ({proposal.rel_path})")
    print(f"Раздел: {proposal.section} | тип: {proposal.unit_type}")
    print(f"Заголовок: {proposal.title}")
    print("\n=== ФАЙЛ ЦЕЛИКОМ ===")
    print(proposal.text)
    print("=== ПРАВКА INDEX.md ===")
    diff = difflib.unified_diff(
        editor.kb.index_text.splitlines(), proposal.new_index_text.splitlines(),
        fromfile="INDEX.md", tofile="INDEX.md (после)", lineterm="", n=1,
    )
    print("\n".join(ln for ln in list(diff)[2:] if ln and ln[0] in "+- @"))


if __name__ == "__main__":
    asyncio.run(main())
