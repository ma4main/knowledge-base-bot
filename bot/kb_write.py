"""Единственное место, где текст базы попадает на диск и в git.

Порядок шагов важен и проверяется в selfcheck:
лок → сверить с диском → записать атомарно → git → перечитать базу и кэш.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from dataclasses import dataclass
from pathlib import Path

import aging

log = logging.getLogger(__name__)

# Стадии, до которых дошла запись: «файл записал, но git не прошёл» и «ничего
# не записал» — разные новости и разные действия.
UNTOUCHED = "нетронуто"
WRITTEN = "записано"
COMMITTED = "закоммичено"


@dataclass
class WriteResult:
    stage: str
    message: str
    sha: str = ""  # коммит правки — по нему работает откат в утреннем отчёте

    @property
    def wrote(self) -> bool:
        return self.stage != UNTOUCHED

    @property
    def ok(self) -> bool:
        return self.stage == COMMITTED


def write_atomic(path: Path, text: str) -> None:
    """Запись одним движением: во временный файл рядом, потом переименование."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def apply_edit(kb, publisher, proposal, message: str) -> WriteResult:
    """Записывает правку единицы и коммитит. Синхронный I/O — звать через to_thread.

    Вызывающий держит `botstate.KB_WRITE_LOCK` и после успеха сам перечитывает
    базу (`kb.reload()`) и синхронизирует кэш ответов — под тем же локом.
    """
    path = kb.root / proposal.rel_path
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as error:
        return WriteResult(UNTOUCHED, f"не смог прочитать {proposal.rel_path}: {error}")
    if current != proposal.old_text:
        return WriteResult(
            UNTOUCHED,
            f"{proposal.kb_id} изменилась с момента подготовки правки "
            f"(кто-то поправил файл или подтянулись чужие коммиты). "
            f"Ничего не записал — повтори правку, я пересоберу её на свежей версии.",
        )
    write_atomic(path, proposal.new_text)
    return _commit([proposal.rel_path], publisher, note_prefix=message)


def apply_new_unit(kb, publisher, proposal, message: str) -> WriteResult:
    """Создаёт новую единицу: файл + строка в каталоге одним коммитом."""
    path = kb.root / proposal.rel_path
    index_path = kb.root / proposal.index_rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return WriteResult(UNTOUCHED, f"файл {proposal.rel_path} уже существует — не перезаписываю")
    try:
        if index_path.read_text(encoding="utf-8") != proposal.old_index_text:
            return WriteResult(UNTOUCHED, (
                "каталог INDEX.md изменился с момента подготовки — ничего не записал. "
                "Повтори, я пересоберу единицу на свежем каталоге."
            ))
    except OSError as error:
        return WriteResult(UNTOUCHED, f"не смог прочитать каталог: {error}")

    write_atomic(path, proposal.text)
    try:
        write_atomic(index_path, proposal.new_index_text)
    except OSError as error:
        path.unlink(missing_ok=True)
        return WriteResult(UNTOUCHED, f"не смог записать каталог ({error}) — единицу откатил")

    return _commit([proposal.rel_path, proposal.index_rel_path], publisher, note_prefix=message)


def mark_unverified(kb, publisher, kb_id: str, message: str) -> WriteResult:
    """Помечает единицу `status: needs-check` по возражению человека. Не откат: текст остаётся."""
    unit = kb.get(kb_id)
    if unit is None:
        return WriteResult(UNTOUCHED, f"единицы {kb_id} в базе нет")
    path = unit.path
    try:
        rel_path = str(path.relative_to(kb.root)).replace(os.sep, "/")
    except ValueError:
        return WriteResult(UNTOUCHED, f"{kb_id} лежит вне репозитория — не трогаю")
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as error:
        return WriteResult(UNTOUCHED, f"не смог прочитать {rel_path}: {error}")

    updated, changed = _set_needs_check(current)
    # Метка спора: без неё возражение человека неотличимо от пометки, которую бот
    # поставил сам, и через месяц «состарилось» бы вместе с ней (aging.py).
    if aging.DISPUTE_MARK not in updated:
        updated = (
            updated.rstrip("\n") + "\n\n"
            + f"{aging.DISPUTE_MARK} помечено непроверенным по возражению {date.today().isoformat()} -->\n"
        )
        changed = True
    if not changed:
        return WriteResult(UNTOUCHED, f"{kb_id} уже помечена как непроверенная")
    write_atomic(path, updated)
    return _commit([rel_path], publisher, note_prefix=message)


def _set_needs_check(text: str) -> tuple[str, bool]:
    """`status: …` → `status: needs-check`. Только вниз: повышать статус автоматически нельзя."""
    match = re.search(r"^status:\s*(.+)$", text or "", re.MULTILINE)
    if match is None or match.group(1).strip() == "needs-check":
        return text, False
    return (
        re.sub(r"^status:\s*.+$", "status: needs-check", text, count=1, flags=re.MULTILINE),
        True,
    )


def _commit(paths: list[str], publisher, note_prefix: str) -> WriteResult:
    """Коммит записанного; sha берётся по разнице HEAD до и после."""
    # Коммит может лечь, а push потом сорваться — sha нужен для отката и в этом случае.
    before = publisher.head_sha()
    ok, note = publisher.commit_paths(paths, note_prefix)
    after = publisher.head_sha()
    sha = after if after and after != before else ""
    return WriteResult(COMMITTED if ok else WRITTEN, note, sha)
