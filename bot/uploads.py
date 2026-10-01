"""Загрузка файлов в библиотеку через самого бота (руководитель).

Поток: руководитель шлёт боту документ → бот сохраняет его в files/ во временное
место и спрашивает описание → руководитель отвечает одним сообщением
(название / теги / описание) → бот дописывает .md и включает файл в библиотеку.
В git файл уходит позже — пачкой, см. publisher.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import slugs

log = logging.getLogger(__name__)

# Максимальный размер: Telegram Bot API отдаёт для скачивания файлы до 20 МБ.
MAX_FILE_MB = 20

def slugify(text: str) -> str:
    """Транслитерация названия для имени файла и id; таблица одна на весь бот (slugs.py)."""
    return slugs.slugify(text, max_len=50)


@dataclass
class PendingUpload:
    """Файл принят, ждём описание.

    `added_by` — кто добавил (уходит сноской в карточку). `simple` — упрощённый ввод:
    одна фраза вместо трёх строк с названием и тегами. `proposed_by*` — заявки,
    поданные на согласование."""

    staged: Path  # временный путь в files/.staging
    original_name: str
    ext: str
    proposed_by_id: int | None = None
    proposed_by: str = ""
    added_by: str = ""
    simple: bool = False


def parse_meta(text: str) -> tuple[str, list[str], str]:
    """Разбирает ответ руководителя: строка 1 — название, 2 — теги, дальше — описание."""
    lines = [ln.strip() for ln in text.strip().splitlines()]
    lines = [ln for ln in lines if ln]
    title = lines[0] if lines else "Без названия"
    tags: list[str] = []
    description = ""
    if len(lines) >= 2:
        tags = [t.strip() for t in lines[1].split(",") if t.strip()]
    if len(lines) >= 3:
        description = " ".join(lines[2:])
    return title, tags, description


def rewrite_meta(
    files_dir: Path, entry, title: str, tags: list[str], description: str
) -> None:
    """Переписывает карточку файла: название, теги, описание. id и имя бинарника не трогает."""
    path = files_dir / f"{Path(entry.binary).stem if entry.binary else entry.id}.md"
    if not path.is_file():
        # Имя .md могло разойтись с именем бинарника — ищем по id в шапке.
        for candidate in files_dir.glob("*.md"):
            if f"id: {entry.id}\n" in candidate.read_text(encoding="utf-8"):
                path = candidate
                break
        else:
            raise FileNotFoundError(f"описание для {entry.id} не найдено")
    md = (
        f"---\n"
        f"id: {entry.id}\n"
        f"title: {title}\n"
        f"tags: [{', '.join(tags)}]\n"
        f"product: [{', '.join(entry.product)}]\n"
        f"given: {entry.given or date.today().isoformat()}\n"
        f"file: {Path(entry.binary).name if entry.binary else ''}\n"
        f"---\n"
        f"{description or entry.description or 'Загружено через бота.'}\n"
    )
    path.write_text(md, encoding="utf-8")


def footnote(added_by: str) -> str:
    """Сноска карточки: кто и когда добавил файл. Ставит код, а не человек."""
    today = date.today()
    return f"Добавил: {added_by or 'сотрудник'}, {today.day:02d}.{today.month:02d}.{today.year}."


def finalize(
    files_dir: Path, pending: PendingUpload, title: str, tags: list[str], description: str,
    added_by: str = "",
) -> tuple[str, str]:
    """Переносит файл из staging под нормальным именем и пишет описание .md.

    Возвращает (file_id, имя_файла). id уникален: при совпадении добавляем номер.
    """
    slug = slugify(title)
    file_id = f"file-{slug}"
    name = slug
    # Не затираем существующие: копилка версий и защита от коллизий имён.
    suffix = 1
    while (files_dir / f"{name}{pending.ext}").exists() or (files_dir / f"{name}.md").exists():
        suffix += 1
        name = f"{slug}-{suffix}"
        file_id = f"file-{name}"

    final_binary = files_dir / f"{name}{pending.ext}"
    pending.staged.replace(final_binary)

    tags_line = ", ".join(tags)
    body = description or "Загружено через бота."
    who = added_by or pending.added_by
    if who:
        body = body.rstrip() + "\n" + footnote(who)
    md = (
        f"---\n"
        f"id: {file_id}\n"
        f"title: {title}\n"
        f"tags: [{tags_line}]\n"
        f"product: []\n"
        f"given: {date.today().isoformat()}\n"
        f"file: {final_binary.name}\n"
        f"---\n"
        f"{body}\n"
    )
    (files_dir / f"{name}.md").write_text(md, encoding="utf-8")
    log.info("Файл добавлен в библиотеку: %s (%s)", file_id, final_binary.name)
    return file_id, final_binary.name
