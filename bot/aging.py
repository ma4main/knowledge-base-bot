"""Старение оговорки «не подтверждено»: запись, прожившая `AGE_DAYS` дней без
возражений, — обычный факт.

Снимается оговорка, но не авторство («записал бот со слов такого-то»). Единица
с меткой спора (`DISPUTE_MARK`) не стареет; `needs-check` снимается только там,
где его поставил сам бот (`AUTO_MARK`). Чистые функции над текстом, без модели.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

AGE_DAYS = 30

DISPUTE_MARK = "<!-- спор:"
# Метка «needs-check поставил бот сам» (ставит `autonomy.mark_needs_check`);
# без неё статус поставил человек, и снять его может только он.
AUTO_MARK = "<!-- needs-check: авто"

_CLAIM = re.compile(
    r"^<!-- claim (?P<id>\w+) evidence=reported (?P<rest>.*?recorded=(?P<recorded>\d{4}-\d{2}-\d{2})) -->$"
)
_CAVEAT = re.compile(r" \*\((?P<who>[^()]*?) — не подтверждено\)\*")
_STATUS = re.compile(r"^status:\s*needs-check\s*$", re.MULTILINE)
_AUTO_MARK_LINE = re.compile(r"\n?<!-- needs-check: авто[^\n]*-->\n?")


def age_text(text: str, today: date | None = None, days: int = AGE_DAYS) -> tuple[str, int]:
    """Снимает оговорку с записей старше `days` дней; возвращает (текст, сколько снял)."""
    if DISPUTE_MARK in text:
        return text, 0
    today = today or date.today()
    border = (today - timedelta(days=days)).isoformat()
    lines = text.splitlines()
    aged = 0
    for i, line in enumerate(lines):
        found = _CLAIM.match(line.strip())
        if not found or found.group("recorded") > border:
            continue
        lines[i] = (
            f"<!-- claim {found.group('id')} evidence=aged {found.group('rest')} "
            f"aged={today.isoformat()} -->"
        )
        # Оговорка стоит строкой выше (у новой единицы её нет — там пометка на всей теме).
        if i > 0:
            lines[i - 1] = _CAVEAT.sub(
                lambda m: f" *(записал бот со слов: {m.group('who')})*", lines[i - 1], count=1
            )
        aged += 1
    if not aged:
        return text, 0
    out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if "evidence=reported" not in out and AUTO_MARK in out:
        out = _STATUS.sub("status: actual", out, count=1)
        out = _AUTO_MARK_LINE.sub("", out)
    return out, aged
