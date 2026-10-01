"""Раздел полезных ссылок: зум, таблицы результатов, панели.

Хранится отдельно от базы знаний обычным Markdown (`knowledge/LINKS.md`): файл
можно читать и править руками, бот только дописывает и удаляет строки.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from formatting import esc_html

log = logging.getLogger(__name__)

REL_PATH = "knowledge/LINKS.md"

HEADER = """\
# Полезные ссылки

Рабочие ссылки, которые бот отдаёт менеджерам по запросу («дай ссылку на зум»).
Добавляет руководитель через бота («запомни ссылку …») — или руками, это обычный
Markdown. Сюда только то, что нужно постоянно; разовые ссылки из чатов не тащим.

"""

_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Link:
    title: str
    url: str
    tags: list[str]
    added_by: str
    added_at: str

    @property
    def key(self) -> str:
        """Короткий стабильный ключ для callback_data (в неё влезает 64 байта)."""
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:12]

    def as_line(self) -> str:
        parts = [f"- **{self.title}** — {self.url}"]
        if self.tags:
            parts.append(f"теги: {', '.join(self.tags)}")
        if self.added_by:
            parts.append(f"добавил {self.added_by}, {self.added_at}")
        return " · ".join(parts)


class LinkBook:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / REL_PATH
        self.links: list[Link] = []
        self.reload()

    def reload(self) -> None:
        self.links = _parse(self.path.read_text(encoding="utf-8")) if self.path.is_file() else []
        log.info("Полезных ссылок загружено: %d", len(self.links))

    def get(self, key: str) -> Link | None:
        return next((l for l in self.links if l.key == key), None)

    def add(self, title: str, url: str, tags: list[str], author: str) -> tuple[bool, str]:
        """Добавляет ссылку. Возвращает (записали ли, сообщение)."""
        if any(l.url == url for l in self.links):
            return False, "Такая ссылка уже сохранена."
        link = Link(
            title=title.strip() or _title_from_url(url),
            url=url,
            tags=tags,
            added_by=author,
            added_at=date.today().isoformat(),
        )
        self.links.append(link)
        self._write()
        log.info("Добавлена ссылка «%s» (%s) от %s", link.title, link.url, author)
        return True, f"Запомнил: «{link.title}»."

    def remove(self, key: str) -> Link | None:
        link = self.get(key)
        if link is None:
            return None
        self.links = [l for l in self.links if l.key != key]
        self._write()
        log.info("Удалена ссылка «%s» (%s)", link.title, link.url)
        return link

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(link.as_line() for link in self.links)
        self.path.write_text(HEADER + body + "\n", encoding="utf-8")

    def prompt_text(self) -> str:
        """Блок для системного префикса. Пусто, если ссылок нет."""
        if not self.links:
            return ""
        lines = [
            "\n# Полезные ссылки\n",
            "Если менеджер просит ссылку (зум, таблица, панель) — дай нужную из списка ниже, "
            "прямо ссылкой. Ссылок, которых здесь нет, НЕ выдумывай: скажи, что такой "
            "сохранённой ссылки нет.\n",
        ]
        for link in self.links:
            tags = f" (теги: {', '.join(link.tags)})" if link.tags else ""
            lines.append(f"- **{link.title}** — {link.url}{tags}")
        return "\n".join(lines) + "\n"

    def summary(self) -> str:
        """Список для команды /links — по категориям, названия кликабельны."""
        if not self.links:
            return (
                "Полезных ссылок пока нет.\n\n"
                "Добавить: <code>запомни ссылку https://… — зум для планёрок</code>"
            )
        groups: dict[str, list[Link]] = {}
        for link in self.links:
            groups.setdefault(category(link), []).append(link)
        lines = [f"<b>Полезные ссылки</b> ({len(self.links)})", ""]
        for name in CATEGORY_ORDER:
            rows = groups.get(name)
            if not rows:
                continue
            lines.append(f"<b>{name}</b>")
            for link in rows:
                title = esc_html(link.title)
                who = ""
                if link.added_by:
                    day = link.added_at or ""
                    when = f", {day[8:10]}.{day[5:7]}.{day[0:4]}" if len(day) == 10 else ""
                    safe = esc_html(link.added_by)
                    who = f" <i>· добавил {safe}{when}</i>"
                lines.append(f'• <a href="{link.url}">{title}</a>{who}')
            lines.append("")
        lines.append("<i>Нажми на название — ссылка откроется.</i>")
        return "\n".join(lines)


# Категория не хранится в файле — определяется по названию, тегам и адресу.
# Порядок проверки важен: «Шаблон партнёрской таблицы» должен попасть
# в шаблоны, а не в таблицы.
_CATEGORY_KEYS: list[tuple[str, tuple[str, ...]]] = [
    ("Зумы", ("зум", "zoom")),
    ("Презентации и шаблоны", ("презентац", "шаблон", "presentation")),
    ("Таблицы", ("таблиц", "spreadsheets", "база знаний")),
    ("Инструкции и гайды", ("инструкц", "гайд")),
]
_OTHER = "Инструменты и прочее"
CATEGORY_ORDER = [name for name, _ in _CATEGORY_KEYS] + [_OTHER]


def category(link: Link) -> str:
    hay = " ".join([link.title, " ".join(link.tags), link.url]).lower()
    for name, keys in _CATEGORY_KEYS:
        if any(key in hay for key in keys):
            return name
    return _OTHER


def _parse(text: str) -> list[Link]:
    """Разбирает файл ссылок. Терпимо к ручным правкам: достаточно строки списка со ссылкой."""
    links: list[Link] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        match = _URL_RE.search(line)
        if not match:
            continue
        url = match.group(0).rstrip(").,;·")
        if url in seen:
            continue
        seen.add(url)
        title_match = re.search(r"\*\*(.+?)\*\*", line)
        if title_match:
            title = title_match.group(1).strip()
        else:
            title = line[2 : match.start()].strip(" —-·:").strip()
        tags_match = re.search(r"теги:\s*([^·\n]+)", line)
        tags = (
            [t.strip() for t in tags_match.group(1).split(",") if t.strip()]
            if tags_match else []
        )
        author_match = re.search(r"добавил\s+([^,·\n]+)(?:,\s*([\d-]+))?", line)
        links.append(
            Link(
                title=title or _title_from_url(url),
                url=url,
                tags=tags,
                added_by=(author_match.group(1).strip() if author_match else ""),
                added_at=(author_match.group(2) or "" if author_match else ""),
            )
        )
    return links


def _title_from_url(url: str) -> str:
    """Запасное название по домену, если описание не дали."""
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return host or "ссылка"


def parse_command(text: str) -> tuple[str, str, list[str]] | None:
    """Разбирает «запомни ссылку https://… — описание; теги: …» в (название, url, теги); None — если ссылки нет."""
    match = _URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(").,;")
    rest = (text[: match.start()] + " " + text[match.end() :]).strip()
    tags: list[str] = []
    tags_match = re.search(r"теги:\s*(.+)$", rest, flags=re.IGNORECASE)
    if tags_match:
        tags = [t.strip().lower() for t in tags_match.group(1).split(",") if t.strip()][:6]
        rest = rest[: tags_match.start()]
    title = rest.strip(" —-·:;,").strip()
    return title, url, tags
