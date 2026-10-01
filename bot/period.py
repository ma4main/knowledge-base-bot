"""Сводка «что изменилось за период». Данные собирает код: история git по
`knowledge/`, горизонт единиц (закрытые и открытые гипотезы), инциденты, новые
материалы. Пересказ пишет модель — только по присланному тексту правок.
"""

from __future__ import annotations

import logging
import re
import kbconfig
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from kb import KB_ID_RE, HORIZON_EXPERIMENT, HORIZON_INCIDENT

log = logging.getLogger(__name__)

# Окна сводки, дней.
WINDOWS = (7, 14)

# Префиксы коммитов-правок базы (по подтверждению человека, автономные, ручные).
# Служебные коммиты (документация, код) в сводку не идут.
BOT_PREFIXES = ("База (бот):", "База (авто):", "База:")

# Человеческие названия разделов базы.
SECTION_TITLES = kbconfig.CFG.section_titles

# Хвост сообщения коммита (кто подтвердил, откуда пришло) — в сводке не нужен.
_TAIL = re.compile(r"\s*\((?:сводка по чатам|переслано)[^)]*\)|,\s*подтвердил\s+\d+")


# Сколько текста добавленных строк берём с одной единицы и со всего периода:
# без границ одна большая правка вытеснит остальные.
MAX_UNIT_CHARS = 1500
MAX_TOTAL_CHARS = 18000

# Сколько новых материалов (файлов и ссылок) показывать в сводке.
MAX_MATERIALS = 12


@dataclass
class Change:
    """Одна правка базы за период."""
    kb_id: str
    when: str
    what: str
    section: str = ""


@dataclass
class UnitDiff:
    """Что появилось в тексте единицы за период — сам текст, а не заголовок коммита."""
    kb_id: str
    title: str
    section: str
    added: str
    horizon: str = ""
    closed: str = ""
    control_point: str = ""


@dataclass
class PeriodReport:
    days: int
    since: str
    until: str
    changes: list[Change] = field(default_factory=list)
    diffs: list[UnitDiff] = field(default_factory=list)  # что появилось в тексте единиц
    # Сколько единиц изменилось всего: в промпт влезает не всё.
    changed_units: int = 0
    closed: list = field(default_factory=list)   # гипотезы, закрытые за период
    open_now: list = field(default_factory=list)  # открытые гипотезы (все)
    incidents: list = field(default_factory=list)  # события за период
    pending: int = 0  # пунктов сводок, ждущих решения
    materials: list[str] = field(default_factory=list)  # новые файлы и ссылки

    @property
    def is_empty(self) -> bool:
        return not (self.changes or self.closed or self.incidents or self.materials)

    def by_section(self) -> dict[str, list[Change]]:
        """Правки по разделам базы; внутри — по дате, свежие первыми."""
        groups: dict[str, list[Change]] = {}
        for change in self.changes:
            groups.setdefault(change.section or "прочее", []).append(change)
        for rows in groups.values():
            rows.sort(key=lambda c: c.when, reverse=True)
        return groups


def collect(repo: Path, kb, days: int = 7, pending: int = 0, today: str = "") -> PeriodReport:
    """Собирает отчёт за последние `days` дней. Ничего не пишет и не спрашивает модель."""
    today = today or date.today().isoformat()
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    report = PeriodReport(days=days, since=since, until=today, pending=pending)

    for when, subject in _commits(repo, since):
        prefix = next((p for p in BOT_PREFIXES if subject.startswith(p)), None)
        if prefix is None:
            continue  # служебный коммит (код, документация) — не изменение базы
        text = subject[len(prefix):].strip()
        ids = KB_ID_RE.findall(text)
        kb_id = ids[0] if ids else ""
        what = _TAIL.sub("", text)
        # kb-id убираем из текста целиком: менеджеру номера единиц не показываем.
        what = KB_ID_RE.sub("", what)
        what = re.sub(r"\[\s*\]|\(\s*\)", "", what)
        what = re.sub(r"\s{2,}", " ", what).strip(" .,—–«»")
        unit = kb.get(kb_id) if kb_id else None
        report.changes.append(
            Change(kb_id=kb_id, when=when, what=what, section=unit.section if unit else "")
        )

    for unit in kb.units.values():
        if unit.horizon == HORIZON_EXPERIMENT:
            if unit.closed and since <= unit.closed <= today:
                report.closed.append(unit)
            elif not unit.closed:
                report.open_now.append(unit)
        elif unit.horizon == HORIZON_INCIDENT and unit.happened:
            if since <= unit.happened <= today:
                report.incidents.append(unit)

    # Единицы, заведённые за период, — событие уровня закрытой гипотезы; в общей
    # куче их вытесняют правки.
    new_ids = {c.kb_id for c in report.changes if c.kb_id and "новая единица" in c.what}
    report.diffs, report.changed_units = _unit_diffs(repo, kb, since, new_ids)
    report.materials = _materials(repo, since)
    report.open_now.sort(key=lambda u: (u.control_point or "9999", u.id))
    report.closed.sort(key=lambda u: u.closed, reverse=True)
    report.incidents.sort(key=lambda u: u.happened, reverse=True)
    log.info(
        "Сводка за %d дней: правок %d, закрыто гипотез %d, инцидентов %d",
        days, len(report.changes), len(report.closed), len(report.incidents),
    )
    return report


def _unit_diffs(
    repo: Path, kb, since: str, new_ids: set[str] | None = None
) -> tuple[list[UnitDiff], int]:
    """Добавленные строки по каждой изменившейся единице; возвращает (вошедшие
    в лимит, сколько единиц изменилось всего). Берём только `+`: убранное для
    сводки «что нового» вторично, а объём удваивает."""
    base = _run(repo, "rev-list", "-1", f"--before={since}", "HEAD")
    if not base:
        return [], 0
    raw = _run(repo, "diff", "--unified=0", f"{base}..HEAD", "--", "knowledge/")
    if not raw:
        return [], 0

    by_file: dict[str, list[str]] = {}
    current = ""
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if not current or not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:].strip()
        # Служебные строки шапки в сводке не нужны.
        if not body or body.startswith(("updated:", "control_point:", "closed:", "horizon:", "happened:")):
            continue
        by_file.setdefault(current, []).append(body)

    units_by_path = {str(u.path.relative_to(kb.root)).replace("\\", "/"): u for u in kb.units.values()}
    found: list[UnitDiff] = []
    for path, added in sorted(by_file.items()):
        unit = units_by_path.get(path)
        if unit is None:
            continue  # INDEX.md, _gorizont.md и прочее служебное
        if not added:
            # Изменилась только шапка — в счёт «изменилось N единиц» не идёт,
            # иначе после массового апдейта сводка сообщает «изменилась вся база».
            continue
        found.append(UnitDiff(
            kb_id=unit.id, title=unit.title, section=unit.section,
            added="\n".join(added)[:MAX_UNIT_CHARS],
            horizon=unit.horizon, closed=unit.closed, control_point=unit.control_point,
        ))

    # Объём ограничен, поэтому порядок решает, что попадёт в сводку: сначала
    # гипотезы и события, потом остальное.
    def priority(diff: UnitDiff) -> tuple[int, int]:
        if diff.horizon == HORIZON_EXPERIMENT:
            rank = 0 if diff.closed else 1
        elif new_ids and diff.kb_id in new_ids:
            rank = 1  # новая тема за период — наравне с открытой гипотезой
        elif diff.horizon == HORIZON_INCIDENT:
            rank = 2
        else:
            rank = 3
        return (rank, -len(diff.added))  # внутри группы — где больше добавили

    out: list[UnitDiff] = []
    total = 0
    for diff in sorted(found, key=priority):
        if total + len(diff.added) > MAX_TOTAL_CHARS:
            continue
        total += len(diff.added)
        out.append(diff)
    if len(out) < len(found):
        log.info(
            "Сводка: в промпт вошло %d единиц из %d (лимит объёма)", len(out), len(found)
        )
    return out, len(found)


def _run(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=40,
            # В diff по files/ попадают PDF, git отдаёт их байтами.
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        log.warning("git %s не отработал", args[0] if args else "?", exc_info=True)
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _commits(repo: Path, since: str) -> list[tuple[str, str]]:
    """(дата, сообщение) по правкам knowledge/ за период. Ошибка git — пустой список."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since={since}",
             "--pretty=format:%ad|%s", "--date=short", "--", "knowledge/"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        log.warning("git log для сводки не отработал", exc_info=True)
        return []
    if result.returncode != 0:
        log.warning("git log вернул %s: %s", result.returncode, result.stderr[:200])
        return []
    rows = []
    for line in result.stdout.splitlines():
        when, _, subject = line.partition("|")
        if subject.strip():
            rows.append((when.strip(), subject.strip()))
    return rows


def render(report: PeriodReport, story: str = "") -> str:
    """Сводка в Telegram-HTML. `story` — пересказ модели; без него остаётся
    скелет из фактов, собранных кодом."""
    ds = _human_date(report.since)
    du = _human_date(report.until)
    lines = [f"📅 <b>Что изменилось за {report.days} дней</b> ({ds} — {du})", ""]

    if report.is_empty:
        lines.append("За этот период база не менялась и гипотезы не закрывались.")
        return "\n".join(lines)

    if story.strip():
        lines.append(story.strip())
    else:
        lines.append("<i>Пересказ не собрался, показываю по фактам.</i>")
        for section, rows in report.by_section().items():
            lines.append(f"\n<b>{SECTION_TITLES.get(section, section)}</b>")
            for change in rows:
                lines.append(f"• {change.what}")

    if report.open_now:
        lines.append("")
        lines.append("⏳ <b>Сейчас проверяем</b>")
        for unit in report.open_now:
            point = _human_date(unit.control_point) if unit.control_point else "без срока"
            lines.append(f"• {_clip(unit.title, 64)} — итог к {point}")

    # Материалы и «без изменений» печатает код, а не модель: пересказ по своей
    # природе сокращает, а список названий надо отдать как есть.
    if report.materials:
        lines.append("")
        lines.append("📎 <b>Новое у бота</b>")
        files = [m[6:] for m in report.materials if m.startswith("файл:")]
        links = [m[8:] for m in report.materials if m.startswith("ссылка:")]
        for name in files:
            lines.append(f"• файл: {_clip(name, 80)}")
        for name in links:
            lines.append(f"• ссылка: {_clip(name, 80)}")
        lines.append(f"<i>Просите словами: «{kbconfig.CFG.example('file')}».</i>")

    quiet = [
        name for key, name in SECTION_TITLES.items()
        if key not in {diff.section for diff in report.diffs}
    ]
    if quiet:
        lines.append("")
        lines.append(f"🗂 <b>Без изменений:</b> {', '.join(quiet)}.")

    tail = []
    # Считаем правки через бота, а не изменившиеся единицы: после массового
    # апдейта «изменилась вся база» человеку ничего не говорит.
    count = len(report.changes)
    if count:
        tail.append(f"{count} {_plural(count, 'правка', 'правки', 'правок')} через бота")
    if report.changed_units > len(report.diffs) > 0:
        tail.append(
            f"в сводку вошли не все изменения ({len(report.diffs)} из {report.changed_units} единиц)"
        )
    if report.pending:
        tail.append(
            f"{report.pending} "
            f"{_plural(report.pending, 'пункт', 'пункта', 'пунктов')} ждут решения (/hvosty)"
        )
    if tail:
        lines.append("")
        lines.append("<i>За период: " + ", ".join(tail) + "</i>")

    return "\n".join(lines).strip()


def _human_date(iso: str) -> str:
    """ГГГГ-ММ-ДД → ДД.ММ; год не показываем."""
    try:
        parsed = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or "?"
    return f"{parsed.day:02d}.{parsed.month:02d}"


def _clip(text: str, limit: int) -> str:
    """Обрезка по слову, а не по символу."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—–(")
    return (cut or text[:limit]) + "…"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Склонение после числа: 1 пункт, 2 пункта, 5 пунктов."""
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


SUMMARY_SYSTEM = (
    f"Ты пишешь сводку «что изменилось за период» для команды компании «{kbconfig.CFG.company}». "
    "Читает её руководитель и сотрудники — в том числе тот, кто "
    "вернулся из отпуска и хочет за минуту понять, что он пропустил.\n\n"
    "Тебе дают то, что за период ДОБАВИЛОСЬ в единицы базы знаний — куски текста "
    "с указанием, из какой единицы они и какой у неё горизонт.\n\n"
    "# Что должно получиться\n\n"
    "Связный рассказ по темам, а не список правок. **Главное: что произошло и какой "
    "статус стал.** Не «обновлена kb-301», а «в панели была ошибка 5000 — "
    "оказалось, дело в провайдере сервера; показы при этом фиксировались».\n\n"
    "# Структура — строго эта, порядок не меняй\n\n"
    "Раздел, для которого в данных ничего нет, ПРОПУСКАЙ целиком — пустых заголовков "
    "быть не должно.\n\n"
    "<b>🎯 Главное</b>\n"
    "1–3 пункта, которые меняют работу отдела прямо сейчас. Это не пересказ всего, "
    "а ответ на вопрос «если прочитать только три строки — что нужно знать». "
    "Закрытая гипотеза почти всегда сюда.\n\n"
    "<b>✅ Решено</b>\n"
    "Что закрыли и **с каким выводом**: гипотезы, споры, расхождения. Вывод обязателен — "
    "«закрыли гипотезу» без результата бесполезно.\n\n"
    "<b>🔧 Изменилось в работе</b>\n"
    "Новые правила и порядок: что теперь делать иначе, к кому обращаться, где что "
    "лежит. Здесь то, что менеджер применит завтра.\n\n"
    "<b>⏳ В работе</b>\n"
    "Что проверяем и к какому сроку. Одной строкой на гипотезу.\n\n"
    "<b>⚠️ Требует внимания</b>\n"
    "Расхождения, спорное, помеченное как «на подтверждении», нерешённое. Если в данных "
    "два источника говорят разное — это сюда, с обеими версиями.\n\n"
    # Разделы «Новые материалы» и «Без изменений» модель не пишет — их печатает
    # render() по данным; здесь только запрет дублировать. Пояснения в текст промпта
    # не выносятся: модель принимала их за комментарий и раздел пропускала.
    "НЕ добавляй разделов про новые файлы, ссылки и про разделы без изменений — "
    "их печатает код после твоего текста. Не дублируй.\n\n"
    "# Про задачи отдела\n\n"
    "Если в добавленном тексте есть ЗАДАЧА, поставленная отделу или менеджерам "
    "(«с каждого по одному проекту на аудит», «до пятницы актуализируйте таблицу»), "
    "вынеси её отдельным пунктом в «Изменилось в работе» и назови срок, если он есть. "
    "Для вернувшегося из отпуска это первое, что нужно: не что поменялось в тексте "
    "базы, а что теперь с него спросят.\n\n"
    "# Даты: не путай, когда произошло и когда записали\n\n"
    "В данных встречается и то и другое. Если в тексте не сказано прямо, КОГДА "
    "событие случилось, не подставляй дату правки базы: день, когда о событии "
    "рассказали, и день, когда оно произошло, — разные даты. Не знаешь даты события — "
    "пиши без даты.\n\n"
    "# Агрегируй, а не перечисляй\n\n"
    "Главное требование. **Пять правок про одно — это ОДИН пункт.** Если за период "
    "трижды дописывали про один процесс (что нужно, сколько занимает, какие доступы) — "
    "пиши одним пунктом: «по процессу сложился порядок: … ». Не «добавлено требование "
    "доступа» отдельной строкой.\n"
    "Если несколько правок складываются в одну историю (была проблема → нашли причину → "
    "починили) — расскажи её как историю, а не тремя пунктами.\n\n"
    "# Как писать\n\n"
    "1. **Человеческим языком, а не заголовками правок.** Каждый пункт — одно-два "
    "предложения, из которых понятно и что случилось, и что теперь делать.\n"
    "2. **Аббревиатуры не расшифровывай наугад.** Внутренние сокращения "
    "пиши ровно так, как в данных. Расшифровку давай, ТОЛЬКО если она есть в "
    "присланном тексте дословно: выдуманная расшифровка потом расходится по команде "
    "как факт. Не знаешь — не расшифровывай, это нормально.\n"
    "3. **Проблема → статус.** Если что-то сломалось и починилось — так и скажи. "
    "Если не до конца («причина известна, решения нет») — скажи и это.\n"
    "4. Без вводных, без «в этот период произошло много важного», без оценок вроде "
    "«отличная новость». Сухо, но по-человечески.\n"
    "5. Разметка Telegram: <b>жирный</b> для заголовков разделов, «•» для пунктов. "
    "Заголовки уровня # не используй.\n"
    "6. **Объём — до 18 строк на всё.** Не помещается — значит недостаточно "
    "обобщил: сворачивай однотипное, выбрасывай мелочь. Длинная сводка не читается, "
    "а несколько правок «для полноты» её убивают.\n"
    "7. Без вступления «вот сводка изменений за период» — человек и так знает, "
    "что открыл. Начинай сразу с раздела.\n\n"
    "# Железное правило\n\n"
    "⚠️ Пиши ТОЛЬКО то, что есть в присланных данных. Ни одной цифры, даты, причины "
    "или вывода, которых там нет. Не додумывай, чем закончилась история, если в данных "
    "этого не сказано — тогда так и пиши: «итог пока не подведён». Эту сводку читают, "
    "чтобы принимать решения."
)


def summary_prompt(report: PeriodReport) -> str:
    """Данные для сводки: добавленный текст единиц, статусы гипотез, материалы."""
    blocks: list[str] = []
    for diff in report.diffs:
        title = SECTION_TITLES.get(diff.section, diff.section)
        mark = ""
        if diff.horizon == HORIZON_EXPERIMENT:
            mark = (
                f" [ГИПОТЕЗА, закрыта {diff.closed}]" if diff.closed
                else f" [ГИПОТЕЗА, открыта, итог к {diff.control_point or 'без срока'}]"
            )
        elif diff.horizon == HORIZON_INCIDENT:
            mark = " [СОБЫТИЕ]"
        blocks.append(
            f"=== {title} · «{diff.title}»{mark} ===\nДобавлено за период:\n{diff.added}"
        )

    tail: list[str] = []
    for unit in report.closed:
        tail.append(f"- гипотеза ЗАКРЫТА {unit.closed}: {unit.title}")
    for unit in report.open_now:
        tail.append(
            f"- гипотеза открыта, итог к {unit.control_point or 'без срока'}: {unit.title}"
        )
    for unit in report.incidents:
        tail.append(f"- событие {unit.happened}: {unit.title}")

    parts = [f"Период: последние {report.days} дней ({report.since} — {report.until})."]
    if blocks:
        parts.append("\n\n".join(blocks))
    if tail:
        parts.append("Состояние гипотез и событий:\n" + "\n".join(tail))
    if report.materials:
        parts.append("Новые материалы у бота за период:\n"
                     + "\n".join(f"- {item}" for item in report.materials))
    # Разделы без изменений называем явно: иначе непонятно, тихо там или сводка
    # их потеряла.
    touched = {diff.section for diff in report.diffs}
    quiet = [name for key, name in SECTION_TITLES.items() if key not in touched]
    if quiet:
        parts.append("Разделы без изменений за период: " + ", ".join(quiet) + ".")
    if not blocks and not tail and not report.materials:
        parts.append("Изменений нет.")
    return "\n\n".join(parts)


# --- «Что изменилось, пока меня не было» — распознавание вопроса --------------
#
# Обычный путь «ответ по единицам» подбирает единицы по смыслу вопроса и понятия
# периода не имеет, поэтому такой вопрос уводится в сводку за период. Распознаём
# узко и без модели: нужен признак периода И признак «что изменилось» — «что нового
# по <продукту>» это вопрос про продукт, а не про период.

_CHANGE_WORDS = re.compile(
    r"что\s+(я\s+)?(важного\s+)?(измен|нов|произошл|поменял|пропустил)"
    r"|чего\s+я\s+не\s+знаю"
    r"|какие\s+(были\s+)?(измен|новост)"
    r"|введи\s+в\s+курс",
    re.IGNORECASE,
)
_AWAY_WORDS = re.compile(
    r"отпуск|меня\s+не\s+было|не\s+было\s+меня|отсутствовал"
    r"|болел|вернул(ся|ась)|на\s+больничном",
    re.IGNORECASE,
)
_MONTHS = {
    "янва": 1, "февр": 2, "март": 3, "марта": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "авгус": 8, "сентяб": 9, "октяб": 10, "нояб": 11, "декаб": 12,
}

MAX_PERIOD_DAYS = 90


# Периоды словами. Проверяются по порядку — специфичные раньше общих.
_WORD_PERIODS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"полтор[аы]\s+месяц"), 45),
    (re.compile(r"полтор[аы]\s+недел"), 11),
    (re.compile(r"(?:две|2)\s+недел"), 14),
    (re.compile(r"(?:три|3)\s+недел"), 21),
    (re.compile(r"пар[уы]\s+недел"), 14),
    (re.compile(r"месяц"), 30),
    (re.compile(r"(?:послед\w+\s+|эту\s+)?недел[юяие]"), 7),
]


def asked_period(text: str, today: date | None = None) -> int | None:
    """Сколько дней показать, если это вопрос «что изменилось за период»; None —
    вопрос не про период. Нужен либо явный срок, либо слова об отсутствии: одного
    признака «что нового» не хватает. Срок не назван, но человека не было — 7 дней."""
    if not text:
        return None
    low = text.lower()
    away = bool(_AWAY_WORDS.search(low))
    change = bool(_CHANGE_WORDS.search(low))
    if not change and not away:
        return None
    today = today or date.today()

    # «с 10 по 16 августа» / «с 10 августа» — считаем от названной даты до сегодня.
    start = re.search(r"с\s+(\d{1,2})\s*(?:по\s+\d{1,2}\s*)?([а-я]{3,})", low)
    if start:
        day = int(start.group(1))
        month = next((num for key, num in _MONTHS.items()
                      if start.group(2).startswith(key)), None)
        if month and 1 <= day <= 31:
            year = today.year if month <= today.month else today.year - 1
            try:
                since = date(year, month, day)
            except ValueError:
                since = None
            if since is not None and since <= today:
                return min(max((today - since).days, 1), MAX_PERIOD_DAYS)

    number = re.search(r"за\s+(\d{1,3})\s*(дн|недел|месяц)", low)
    if number:
        count = int(number.group(1))
        unit = number.group(2)
        days = count * (1 if unit == "дн" else 7 if unit == "недел" else 30)
        return min(max(days, 1), MAX_PERIOD_DAYS)
    # Срок словами ловим и без «за» («не было меня полторы недели»).
    for pattern, days in _WORD_PERIODS:
        if pattern.search(low):
            return min(days, MAX_PERIOD_DAYS)
    return 7 if away else None


def _materials(repo: Path, since: str) -> list[str]:
    """Новые файлы библиотеки и полезные ссылки за период (files/*.md, LINKS.md)."""
    base = _run(repo, "rev-list", "-1", f"--before={since}", "HEAD")
    if not base:
        return []
    raw = _run(repo, "diff", "--unified=0", f"{base}..HEAD", "--",
               "files/*.md", "knowledge/LINKS.md")
    out: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:].strip()
        if not body:
            continue
        # Описание файла: заголовок из шапки.
        if body.startswith("title:"):
            out.append("файл: " + body[6:].strip())
            continue
        # Строка полезной ссылки: «- **Название** — https://…».
        if body.startswith("- **"):
            name = body[4:].split("**", 1)[0].strip()
            if name:
                out.append("ссылка: " + name)
    # Одну и ту же строку могли править дважды за период.
    seen: set[str] = set()
    unique = [x for x in out if not (x in seen or seen.add(x))]
    return unique[:MAX_MATERIALS]
