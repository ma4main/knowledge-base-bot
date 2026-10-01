"""Доступ к базе знаний: каталог, загрузка единиц, поиск по словам.

База — обычные Markdown-файлы в git. Слой намеренно тонкий: если бот когда-нибудь
будет заменён другой оболочкой, ядро останется читаемым без него.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

# В базе встречаются и трёх-, и четырёхзначные номера.
KB_ID_RE = re.compile(r"\bkb-\d{3,4}\b")


# Горизонт: как долго живёт знание в единице.
# rule       — правило; действует, пока не заменили (старое остаётся с датами).
# experiment — гипотеза; обязана закрыться выводом в контрольную точку.
# incident   — событие (сбой, апдейт, откат): строка журнала, а не знание.
# Явно помечается только не-правило, отсюда значение по умолчанию.
HORIZON_RULE = "rule"
HORIZON_EXPERIMENT = "experiment"
HORIZON_INCIDENT = "incident"
HORIZONS = (HORIZON_RULE, HORIZON_EXPERIMENT, HORIZON_INCIDENT)


@dataclass(frozen=True)
class Unit:
    id: str
    title: str
    section: str
    status: str
    path: Path
    text: str
    # Горизонт и его даты. `control_point` — когда подводим итог по гипотезе,
    # `closed` — когда итог подведён (пусто = гипотеза открыта), `happened` —
    # когда случилось событие.
    horizon: str = HORIZON_RULE
    control_point: str = ""
    closed: str = ""
    happened: str = ""

    @property
    def is_open_experiment(self) -> bool:
        """Гипотеза, у которой ещё не подведён итог."""
        return self.horizon == HORIZON_EXPERIMENT and not self.closed


class KnowledgeBase:
    """Загружается при старте целиком и держится в памяти."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.index_path = root / "knowledge" / "INDEX.md"
        self.index_text = self.index_path.read_text(encoding="utf-8")
        self.units: dict[str, Unit] = {}
        self._load()

    def reload(self) -> None:
        """Перечитать базу после правки через бота — без перезапуска процесса."""
        self.index_text = self.index_path.read_text(encoding="utf-8")
        self.units.clear()
        self._load()

    def _load(self) -> None:
        for path in sorted((self.root / "knowledge").rglob("kb-*.md")):
            text = path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            unit_id = fm.get("id") or path.stem.split("-", 2)[:2]
            if not isinstance(unit_id, str):
                unit_id = "-".join(unit_id)
            horizon = str(fm.get("horizon") or HORIZON_RULE).strip().lower()
            if horizon not in HORIZONS:
                log.warning(
                    "%s: неизвестный horizon %r — считаю правилом", unit_id, horizon
                )
                horizon = HORIZON_RULE
            self.units[unit_id] = Unit(
                id=unit_id,
                title=fm.get("title", path.stem),
                section=fm.get("section", path.parent.name),
                status=fm.get("status", "unknown"),
                path=path,
                text=text,
                horizon=horizon,
                control_point=str(fm.get("control_point") or "").strip(),
                closed=str(fm.get("closed") or "").strip(),
                happened=str(fm.get("happened") or "").strip(),
            )
        log.info("База знаний загружена: %d единиц из %s", len(self.units), self.root)
        experiments = self.open_experiments()
        if experiments:
            log.info(
                "Открытых гипотез: %d (%s)",
                len(experiments), ", ".join(u.id for u in experiments),
            )

    def disk_fingerprint(self) -> tuple[int, int]:
        """Дешёвый отпечаток файлов базы на диске: (сколько файлов, последняя правка). Читает только метаданные."""
        newest = 0
        count = 0
        for path in (self.root / "knowledge").rglob("*.md"):
            try:
                newest = max(newest, path.stat().st_mtime_ns)
            except OSError:
                continue  # файл исчез между обходом и stat — заметим на следующем круге
            count += 1
        return count, newest

    def open_experiments(self, today: str = "") -> list[Unit]:
        """Гипотезы без вывода — сначала просроченные, потом остальные."""
        today = today or date.today().isoformat()
        open_units = [u for u in self.units.values() if u.is_open_experiment]
        # Просроченные вперёд: контрольная точка прошла, а вывода нет.
        return sorted(
            open_units,
            key=lambda u: (
                not (u.control_point and u.control_point <= today),
                u.control_point or "9999",
                u.id,
            ),
        )

    def by_horizon(self, horizon: str) -> list[Unit]:
        return sorted(
            (u for u in self.units.values() if u.horizon == horizon), key=lambda u: u.id
        )

    def unit_hashes(self) -> dict[str, str]:
        """Отпечаток каждой единицы отдельно, {id: sha1[:12]} — по тексту, а не по счётчику правок."""
        return {
            unit_id: hashlib.sha1(unit.text.encode("utf-8")).hexdigest()[:12]
            for unit_id, unit in self.units.items()
        }

    def get(self, unit_id: str) -> Unit | None:
        return self.units.get(unit_id.strip().lower())

    def get_many(self, ids: list[str]) -> tuple[list[Unit], list[str]]:
        """Возвращает найденные единицы и список ненайденных id."""
        found, missing = [], []
        for raw in ids:
            unit = self.get(raw)
            (found.append(unit) if unit else missing.append(raw))
        return found, missing

    def search(
        self, query: str, limit: int = 8, stem: bool = False,
        include_outdated: bool = False,
    ) -> list[Unit]:
        """Запасной поиск по словам, когда маршрутизации по INDEX не хватило.

        `stem=True` — грубая нормализация окончаний (слово обрезается до пяти букв)
        для запросов в живой форме; по умолчанию выключено. `include_outdated=False` —
        устаревшие единицы в кандидаты не идут.
        """
        words = [w for w in re.findall(r"\w{4,}", query.lower())]
        if stem:
            words = sorted({w[:5] if len(w) > 5 else w for w in words})
        if not words:
            return []
        scored: list[tuple[int, Unit]] = []
        for unit in self.units.values():
            if not include_outdated and unit.status == "outdated":
                continue
            haystack = unit.text.lower()
            # Заголовок весит больше тела: попадание в название почти всегда точнее.
            score = sum(haystack.count(w) for w in words)
            score += 10 * sum(w in unit.title.lower() for w in words)
            if score:
                scored.append((score, unit))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [unit for _, unit in scored[:limit]]
