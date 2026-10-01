"""Библиотека файлов: презентации, документы, инструкции.

Каждому файлу соответствует Markdown-описание `files/<имя>.md` с шапкой, рядом
лежит сам файл. Telegram file_id не хранится: он привязан к конкретному боту.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from frontmatter import parse_frontmatter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileEntry:
    id: str
    title: str
    tags: list[str]
    product: list[str]
    given: str  # дата выдачи, ISO
    description: str
    binary: Path | None  # None, если описание есть, а файл ещё не загружен

    @property
    def available(self) -> bool:
        return self.binary is not None and self.binary.is_file()

    @property
    def filename(self) -> str:
        return self.binary.name if self.binary else "(файл ещё не загружен)"


def _list(raw: str) -> list[str]:
    return [x.strip() for x in raw.strip("[] ").split(",") if x.strip()]


class FileLibrary:
    """Загружается при старте. Файлов немного — держим описания в памяти."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "files"
        self.entries: dict[str, FileEntry] = {}
        self._load()

    def reload(self) -> None:
        """Перечитать после загрузки нового файла — без перезапуска бота."""
        self.entries.clear()
        self._load()

    def showcase(self, limit: int = 30) -> str:
        """Витрина: что есть в библиотеке — названиями, без описаний. Пусто, если файлов нет."""
        ready = sorted(
            (e for e in self.entries.values() if e.available), key=lambda e: e.title.lower()
        )
        # Описан, но не загружен — показываем отдельно.
        promised = sorted(
            (e for e in self.entries.values() if not e.available), key=lambda e: e.title.lower()
        )
        if not ready and not promised:
            return ""
        lines = [f"<b>Файлы</b> ({len(ready)})"]
        for entry in ready[:limit]:
            lines.append(f"• {entry.title}")
        if len(ready) > limit:
            lines.append(f"…и ещё {len(ready) - limit}")
        if promised:
            lines.append("")
            lines.append("<i>Описаны, но файла ещё нет: " + ", ".join(
                e.title for e in promised[:5]
            ) + "</i>")
        return "\n".join(lines)

    def _safe_binary(self, name: str, file_id: str) -> Path | None:
        """Путь к бинарнику, только если он строго внутри files/ (защита от path traversal). Иначе None."""
        if not name:
            return None
        if "/" in name or "\\" in name or ".." in name:
            log.warning("У %s небезопасное имя файла %r — игнорирую", file_id, name)
            return None
        candidate = (self.dir / name).resolve()
        if not candidate.is_relative_to(self.dir.resolve()):
            log.warning("У %s путь файла вне files/ (%r) — игнорирую", file_id, name)
            return None
        if not candidate.is_file():
            log.warning("У %s описан файл %s, но его нет на диске", file_id, name)
            return None
        return candidate

    def _load(self) -> None:
        if not self.dir.is_dir():
            log.info("Папки files/ нет — библиотека файлов пуста")
            return
        for md in sorted(self.dir.glob("*.md")):
            if md.name in {"README.md", "INDEX.md"}:
                continue
            meta = parse_frontmatter(md.read_text(encoding="utf-8"), with_body=True)
            file_id = meta.get("id") or md.stem
            binary = self._safe_binary(meta.get("file", ""), file_id)
            self.entries[file_id] = FileEntry(
                id=file_id,
                title=meta.get("title", md.stem),
                tags=_list(meta.get("tags", "")),
                product=_list(meta.get("product", "")),
                given=meta.get("given", ""),
                description=meta.get("__body__", ""),
                binary=binary,
            )
        log.info("Библиотека файлов: %d описаний, из них с файлом %d",
                 len(self.entries), sum(e.available for e in self.entries.values()))

    def get(self, file_id: str) -> FileEntry | None:
        return self.entries.get(file_id.strip())

    def search(self, query: str, limit: int = 6) -> list[FileEntry]:
        """Поиск по заголовку, тегам, продукту и описанию; при равной релевантности свежие первыми."""
        words = re.findall(r"\w{3,}", query.lower())
        scored: list[tuple[int, str, FileEntry]] = []
        for entry in self.entries.values():
            hay = " ".join(
                [entry.title, " ".join(entry.tags), " ".join(entry.product), entry.description]
            ).lower()
            score = sum(hay.count(w) for w in words)
            score += 5 * sum(w in entry.title.lower() for w in words)
            score += 5 * sum(w in " ".join(entry.tags).lower() for w in words)
            if score:
                scored.append((score, entry.given, entry))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [e for _, _, e in scored[:limit]]
