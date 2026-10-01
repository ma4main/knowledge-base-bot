"""Справочник людей: кто есть кто и как его зовут.

Две части разной надёжности, которые не смешиваются: подтверждённый человеком
`knowledge/PEOPLE.md` (имена и зоны ответственности, правится руками) и наблюдённые
подписи из Telegram (`@ник` → имя профиля, копятся в состоянии бота). Из них
собирается блок для системного префикса; главное в нём — право не знать:
нет пары в справочнике — пиши `@ник`.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

REL_PATH = "knowledge/PEOPLE.md"

# Сколько наблюдённых подписей держим в промпте.
MAX_OBSERVED = 40

# Правило про имена лежит здесь, а не в prompts.py: менять его надо там же, где справочник.
NAMING_RULE = (
    "# Люди: имена не выдумывать\n\n"
    "**Никогда не расшифровывай `@ник` в имя, если пары нет в справочнике ниже.** "
    "Не догадывайся по звучанию ника, по контексту разговора и по тому, кто обычно "
    "пишет в этом чате. Не знаешь, кто это — так и пиши `@ник`.\n"
    "То же про роли: кто чем занимается — только из справочника. Не назначай людям "
    "задачи и не обещай за них («коллега сделает к пятнице»).\n"
)


class PeopleBook:
    """Справочник для промпта: подтверждённая часть из файла, наблюдённая — из состояния бота (`State.people`)."""

    def __init__(self, root: Path, state=None) -> None:
        self.root = root
        self.path = root / REL_PATH
        self.state = state
        self.text = ""
        self.reload()

    def reload(self) -> None:
        self.text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        if self.text:
            log.info("Справочник людей загружен: %d символов", len(self.text))

    def prompt_text(self) -> str:
        """Блок для системного префикса; обязан быть побайтово одинаковым от запроса к запросу (кэш), поэтому подписи сортируются."""
        # Правило нужно и при пустом справочнике: именно тогда модель достраивает имена.
        parts = ["\n" + NAMING_RULE]
        if self.text.strip():
            parts.append("\n## Справочник (подтверждён руководителем)\n\n" + self.text.strip())
        observed = self._observed_lines()
        if observed:
            parts.append(
                "\n## Как люди подписаны в Telegram\n\n"
                "Это не роли, а только соответствие ника и имени профиля — "
                "чтобы обратиться к человеку правильно.\n\n" + "\n".join(observed)
            )
        return "\n".join(parts) + "\n"

    def _observed_lines(self) -> list[str]:
        seen = getattr(self.state, "people", None) if self.state is not None else None
        if not seen:
            return []
        lines = []
        for nick, name in sorted(seen.items())[:MAX_OBSERVED]:
            if name:
                lines.append(f"- @{nick} — {name}")
        return lines
