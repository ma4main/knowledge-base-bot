"""Автономное пополнение базы. Класс риска правки определяется сначала жёсткими
правилами кода, потом моделью: green/yellow пишутся как есть, red — с пометкой
needs-check, blocked (бракованная правка) не пишется никогда.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date

import aging
import kb_write

log = logging.getLogger(__name__)

# `kb_editor` импортирует этот модуль, поэтому его константы берутся по месту
# вызова: импорт на уровне модуля замкнул бы кольцо.

GREEN = "green"
YELLOW = "yellow"
RED = "red"
# Правка не рискованная, а бракованная: удаляет текст, не прошла перепроверку,
# укоротила единицу. Вопрос целостности правки, а не доверия к факту.
BLOCKED = "blocked"

LABELS = {
    GREEN: "🟢 записал сам",
    YELLOW: "🟡 записал, пометил как авто",
    RED: "🔴 записал как непроверенное (needs-check)",
    BLOCKED: "⛔ не записал — правка бракованная",
}

# Максимум автоправок в сутки; остальное ждёт человека.
DAILY_LIMIT = 10

# Цифры, деньги, проценты, сроки: правка с такой строкой — рискованная.
_NUMERIC = re.compile(
    r"\d+\s*(?:₽|руб|%|процент|клик|переход|запрос|точк|недел|дн|мес|час|мин|тыс)",
    re.IGNORECASE,
)

# Признаки текста про то, что говорят клиенту.
_CLIENT_WORDS = (
    "клиент", "заказчик", "что сказать", "скрипт", "формулировк", "возражени",
)

# Служебные поля шапки, смена которых меняет смысл единицы целиком.
_META_FIELDS = ("status:", "horizon:", "control_point:", "closed:", "sources:")

# Причинно-следственное утверждение («X даёт рост», «из-за Y упало») — почти
# всегда гипотеза, а не факт, даже если произнесено уверенно.
_CAUSAL = re.compile(
    r"\b(да[её]т|при[вн]од(ит|ят)|веду?т\s+к|помога(ет|ют)|влия(ет|ют)|"
    r"обеспечива(ет|ют)|позволя(ет|ют)\s+вырасти|из-за\s+\w+\s+(упал|вырос|прос[её]л))\b"
    r"|=\s*(рост|падени|прос[ая]дк)",
    re.IGNORECASE,
)


def changed_lines(old: str, new: str) -> tuple[list[str], list[str]]:
    """Строки, которые исчезли и появились; сравнение по множествам, перестановка
    строк правкой не считается."""
    before = [ln.strip() for ln in old.splitlines() if ln.strip()]
    after = [ln.strip() for ln in new.splitlines() if ln.strip()]
    removed = [ln for ln in before if ln not in set(after)]
    added = [ln for ln in after if ln not in set(before)]
    return removed, added


def hard_rules(proposal, unit=None, is_new_unit: bool = False) -> tuple[str, str] | None:
    """Класс по жёстким правилам, без модели. None — правила молчат, спросим модель.
    BLOCKED — правка бракованная, не пишется; RED — факт рискованный, пишется
    со `status: needs-check`."""
    removed, added = changed_lines(proposal.old_text, proposal.new_text)

    # --- Бракованная правка: не пишем ни при каких пометках -------------------
    # Строку «updated:» в шапке меняет код, это не правка текста.
    #
    # Замена состояния (`state_change`) — исключение: новое значение вытесняет
    # старое из блока «Сейчас», а старое уезжает в «Было раньше». Для
    # `changed_lines` это выглядит как удаление, но переписывает строку код
    # (`nowblock`) по ключу, и текст не теряется.
    meaningful_removed = [ln for ln in removed if not ln.startswith("updated:")]
    if meaningful_removed and getattr(proposal, "state_change", None) is None:
        return BLOCKED, f"правка удаляет или переписывает текст ({len(meaningful_removed)} строк)"

    if any(field in ln for ln in added for field in _META_FIELDS):
        return BLOCKED, "правка трогает служебные поля шапки (статус, горизонт, источники)"

    if proposal.changed_regions() > 3:
        return BLOCKED, f"задето слишком много мест ({proposal.changed_regions()})"

    if not proposal.audited:
        return BLOCKED, "перепроверка не сработала — правка не проверена на постороннее"

    if proposal.shrinks_a_lot():
        return BLOCKED, "единица заметно укоротилась"

    # --- Рискованный факт: пишем, но помечаем как непроверенный ---------------
    if any(_NUMERIC.search(ln) for ln in added):
        return RED, "в правке есть цифра — деньги, срок или порог"

    low_added = " ".join(added).lower()
    if any(word in low_added for word in _CLIENT_WORDS):
        return RED, "правка задевает то, что говорят клиенту"

    if any(_CAUSAL.search(ln) for ln in added):
        return RED, "утверждение о причине и следствии — это гипотеза, а не факт"

    if unit is not None and getattr(unit, "horizon", "") == "experiment":
        return RED, "единица — гипотеза, вывод по ней подводит человек"

    if is_new_unit:
        # Новая единица меняет каталог, по которому маршрутизируются все будущие
        # правки, — не бывает GREEN.
        return YELLOW, "новая единица"

    return None


# Хвост даты у строки блока «Сейчас» — тот же формат, что ловит nowblock._SINCE.
_NOW_TAIL = re.compile(r"\s*—\s*с\s+\d{2}\.\d{2}\.\d{4}\s*$")
# Строка истории блока «Было раньше» (закрытый интервал «— с … по …») и сам
# заголовок блока.
_HISTORY_LINE = re.compile(r"—\s*с\s+\d{2}\.\d{2}\.\d{4}\s+по\s+\d{2}\.\d{2}\.\d{4}\s*$")
_HISTORY_HEAD = re.compile(r"^\s*(#{1,6}\s*|\*\*)?Было раньше", re.IGNORECASE)


def claim_comment(prov: dict, evidence: str) -> str:
    """Машиночитаемый провенанс claim'а — HTML-комментарий у абзаца (формат:
    spec-claim-uroven.md)."""
    said = (prov.get("said") or "")[:10]
    return (
        f"<!-- claim {uuid.uuid4().hex[:8]} evidence={evidence}"
        f' who="{prov.get("who") or "?"}" who_id={prov.get("who_id") or "?"}'
        f' msg="{prov.get("msg") or "?"}" said={said or "?"}'
        f" recorded={date.today().isoformat()} -->"
    )


def signature(prov: dict) -> str:
    """Видимая подпись под записью, внесённой человеком: «(внёс Имя, ДД.ММ.ГГГГ)»."""
    said = (prov.get("said") or "")[:10]
    day = said if len(said) == 10 else date.today().isoformat()
    who = prov.get("who") or "сотрудник"
    return f" *(внёс {who}, {day[8:10]}.{day[5:7]}.{day[0:4]})*"


def annotate_claim(old_text: str, new_text: str, prov: dict, evidence: str) -> str:
    """Дописывает к добавленному тексту оговорку или подпись и провенанс-комментарий.
    Не нашли подходящей строки — возвращает текст как есть."""
    _, added = changed_lines(old_text, new_text)
    added = [ln for ln in added if not ln.startswith(("updated:", "status:"))]
    if not added:
        return new_text
    # Аннотация вешается на новое значение, а не на строку истории «Было раньше»,
    # которая при замене состояния тоже выглядит добавленной.
    fresh = [ln for ln in added if not _HISTORY_LINE.search(ln) and not _HISTORY_HEAD.match(ln)]
    target = (fresh or added)[-1]
    caveat = ""
    if evidence == "confirmed" and prov.get("signed"):
        caveat = signature(prov)
    elif evidence == "reported":
        said = (prov.get("said") or "")[:10]
        when = f", {said[8:10]}.{said[5:7]}" if len(said) == 10 else ""
        # Имя в именительном падеже, как пришло из Telegram: без «со слов»,
        # чтобы не склонять чужие имена кодом.
        who = prov.get("who") or "сотрудник"
        caveat = f" *({who}{when} — не подтверждено)*"
    lines = new_text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == target:
            # Дата «— с ДД.ММ.ГГГГ» у строк блока «Сейчас» должна остаться в конце
            # строки: nowblock ищет её по концу.
            tail = _NOW_TAIL.search(lines[i])
            if tail and caveat:
                lines[i] = lines[i][: tail.start()].rstrip() + caveat + tail.group(0)
            else:
                lines[i] = lines[i].rstrip() + caveat
            lines.insert(i + 1, claim_comment(prov, evidence))
            out = "\n".join(lines)
            return out + "\n" if new_text.endswith("\n") else out
    return new_text


_STATUS_LINE = re.compile(r"^status:\s*(.+)$", re.MULTILINE)


def mark_needs_check(new_text: str) -> tuple[str, bool]:
    """Ставит в шапке `status: needs-check`, только в сторону понижения.
    Возвращает (текст, поменяли ли)."""
    match = _STATUS_LINE.search(new_text or "")
    if match is None:
        return new_text, False
    current = match.group(1).strip()
    if current == "needs-check":
        return new_text, False
    marked = _STATUS_LINE.sub("status: needs-check", new_text, count=1)
    # Метка «поставил бот»: по ней aging.py потом снимет статус сам; пометку
    # человека (без метки) не тронет.
    marked = marked.rstrip("\n") + f"\n\n{aging.AUTO_MARK} {date.today().isoformat()} -->\n"
    return marked, True


RISK_PROMPT = """\
Ты определяешь, можно ли внести правку в базу знаний БЕЗ человека.

🟢 — факт с датой и однозначным источником, который легко проверить и не жалко \
откатить: адрес системы, к кому обращаться, график, срок, статус инструмента.
🟡 — уточнение существующего правила: смысл прежний, стало точнее.
🔴 — всё, где ошибка дорога: деньги и цифры, слова для клиента, правила работы \
с клиентом, отмена или разворот прежнего правила, что-то спорное.

Ответь ОДНИМ словом: зелёный, жёлтый или красный. Затем со второй строки — \
причина в одной короткой фразе.

Сомневаешься — отвечай на класс выше (жёлтый вместо зелёного, красный вместо \
жёлтого): ошибка в сторону осторожности стоит одного нажатия человека, ошибка \
в другую сторону расходится по отделу как факт.\
"""

def body_lines(text: str, limit: int = 40) -> list[str]:
    """Тело единицы без шапки и пустых строк."""
    lines = (text or "").splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), 0)
        lines = lines[end + 1:]
    return [ln for ln in lines if ln.strip()][:limit]


@dataclass
class Draft:
    """Подготовленная, но ещё не записанная правка: между `draft()` и `commit()`
    её показывают автору."""
    kind: str  # "edit" — правка единицы, "new" — новая единица
    fact: str
    origin: str
    prov: dict | None
    confirmed: bool  # формулировку подтверждает человек (а не ночной автомат)
    proposal: object  # kb_editor.EditProposal или NewUnitProposal
    kb_id: str
    title: str
    level: str = ""
    reason: str = ""
    marked: bool = False  # поставлен needs-check
    evidence: str | None = None
    # Строки, которые появятся в единице, — без служебных комментариев.
    added: list[str] = field(default_factory=list)
    # Строки, которые из единицы уйдут, — человек видит их до подтверждения.
    removed: list[str] = field(default_factory=list)


class AutoWriter:
    """Конвейер автономной правки: факт из чата → класс риска → запись или очередь.
    Любая остановка означает «отдать человеку», а не «выбросить»."""

    def __init__(self, kb, kb_editor, publisher, answer_cache, state, lock) -> None:
        self.kb = kb
        self.kb_editor = kb_editor
        self.publisher = publisher
        self.answer_cache = answer_cache
        self.state = state
        self.lock = lock

    async def consider(
        self, fact: str, origin: str, model: str | None = None,
        prov: dict | None = None,
    ) -> dict:
        """Ночной путь без человека: подготовить и сразу записать.

        `class` в ответе: `green` / `yellow` — записано; `red` — записано
        со `status: needs-check`; `blocked` — правка бракованная, не записано;
        `human` — единицы нет или подходит несколько; `skip` — не похоже на знание;
        `off` — автозапись выключена; `limit` — суточный лимит исчерпан."""
        draft = await self.draft(fact, origin, model=model, prov=prov, confirmed=False)
        if isinstance(draft, dict):
            return draft
        return await self.commit(draft)

    async def draft(
        self, fact: str, origin: str, model: str | None = None,
        prov: dict | None = None, confirmed: bool = False,
    ) -> "Draft | dict":
        """Готовит запись, но не пишет. Возвращает Draft или словарь-отказ (те же
        `class`, что у `consider`: human / blocked / skip / off / limit).

        `confirmed=True` — формулировку подтверждает автор: выключатель и суточный
        лимит не действуют, needs-check не ставится, вместо оговорки — подпись.
        Бракованная правка не пишется и с подтверждением."""
        if not confirmed:
            if not self.state.autonomy:
                return {"class": "off"}
            if self.state.auto_edits_today() >= DAILY_LIMIT:
                return {"class": "limit", "reason": f"суточный лимит {DAILY_LIMIT} исчерпан"}
        if prov is not None and confirmed:
            prov = {**prov, "signed": True}

        candidates, note = await self.kb_editor.pick_units(fact, model=model)
        if not candidates:
            from kb_editor import NO_FITTING_UNIT  # локально: кольцо импортов с kb_editor

            if note != NO_FITTING_UNIT:
                return {"class": "human", "reason": f"не смог подобрать единицу: {note}"}

            # Отмена записанного правила сформулирована не так, как само правило,
            # и маршрутизатор её не находит — проверяем отдельно, иначе в базе
            # окажутся два противоречащих правила.
            cancels, why = await self.kb_editor.cancels_existing(fact, model=model)
            if cancels is not None:
                candidates = [cancels]
                note = f"факт отменяет записанное: {why}"
            else:
                return await self._draft_unit(fact, origin, model, prov, confirmed)

        if len(candidates) > 1:
            chosen, why = await self.kb_editor.choose_one(fact, candidates, model=model)
            if chosen is None:
                return {
                    "class": "human",
                    "reason": f"подходит несколько единиц ({', '.join(c.kb_id for c in candidates)}), "
                              f"выбрать не удалось",
                }
            candidates = [chosen]

        kb_id = candidates[0].kb_id
        proposal, note = await self.kb_editor.prepare(kb_id, fact, model=model)
        if proposal is None:
            return {"class": "human", "reason": f"правка не собралась: {note}"}

        unit = self.kb.get(kb_id)
        verdict = hard_rules(proposal, unit)
        if verdict is None:
            verdict = await self.kb_editor.classify_risk(fact, proposal, model=model)
        level, reason = verdict

        # Бракованная правка автоматом не пишется; с подтверждением идёт дальше,
        # а убираемые строки уезжают в черновик, чтобы автор их увидел.
        if level == BLOCKED:
            if not confirmed:
                return {"class": BLOCKED, "reason": reason, "kb_id": kb_id}
            level, reason = YELLOW, f"меняет существующий текст ({reason}) — подтверждает автор"

        marked = False
        if level == RED and not confirmed:
            proposal.new_text, marked = mark_needs_check(proposal.new_text)

        # Снимок изменений — до аннотации, чтобы показать человеку чистый текст.
        removed, added = changed_lines(proposal.old_text, proposal.new_text)

        # Оговорку и провенанс ставит код после классификации риска, чтобы
        # аннотация не влияла на класс.
        evidence = None
        if prov:
            evidence = "confirmed" if confirmed else "reported"
            proposal.new_text = annotate_claim(
                proposal.old_text, proposal.new_text, prov, evidence
            )

        return Draft(
            kind="edit", fact=fact, origin=origin, prov=prov, confirmed=confirmed,
            proposal=proposal, kb_id=kb_id,
            title=getattr(unit, "title", "") or kb_id,
            level=level, reason=reason, marked=marked, evidence=evidence,
            added=[ln for ln in added if not ln.startswith(("updated:", "status:"))],
            removed=[ln for ln in removed if not ln.startswith(("updated:", "status:"))],
        )

    async def _draft_unit(
        self, fact: str, origin: str, model: str | None,
        prov: dict | None, confirmed: bool,
    ) -> "Draft | dict":
        """Новая единица под факт, которому не нашлось места. От автомата — всегда
        с needs-check: новая единица меняет каталог маршрутизации всех будущих правок."""
        worth, why = await self.kb_editor.worth_new_unit(fact, model=model)
        if not worth:
            log.info("Новую единицу не завожу (%s): %s", why, fact[:80])
            return {"class": "skip", "reason": f"не похоже на знание для базы: {why}"}

        author = (prov or {}).get("who") if confirmed else None
        try:
            proposal, note = await self.kb_editor.propose_new(
                fact, author=author or "бота (автоматически)", model=model, origin=origin
            )
        except Exception:
            log.exception("Новая единица не собралась: %s", fact[:80])
            return {"class": "human", "reason": "новая тема, но единица не собралась"}
        if proposal is None:
            return {"class": "human", "reason": f"новая тема, единица не собралась: {note}"}

        body = body_lines(proposal.text)
        if not confirmed:
            proposal.text, _ = mark_needs_check(proposal.text)
        # Провенанс новой единицы — в конце файла. От автомата оговорка в текст
        # не вставляется: вся единица и так под needs-check.
        evidence = None
        if prov:
            evidence = "confirmed" if confirmed else "reported"
            tail = (signature(prov).strip() + "\n\n") if confirmed else ""
            proposal.text = (
                proposal.text.rstrip() + "\n\n" + tail + claim_comment(prov, evidence) + "\n"
            )
        return Draft(
            kind="new", fact=fact, origin=origin, prov=prov, confirmed=confirmed,
            proposal=proposal, kb_id=proposal.kb_id, title=proposal.title,
            level=YELLOW,
            reason="новая тема" + ("" if confirmed else ", завёл сам и пометил needs-check"),
            marked=not confirmed, evidence=evidence, added=body,
        )

    async def commit(self, draft: "Draft") -> dict:
        """Записывает черновик; возвращает запись о сделанном (те же поля, что у `consider`)."""
        proposal = draft.proposal
        who = (draft.prov or {}).get("who") or ""
        if draft.kind == "new":
            message = (
                f"База (бот): новая единица {proposal.kb_id} «{proposal.title}» — "
                f"{draft.origin}, формулировку подтвердил {who}"
                if draft.confirmed else
                f"База (авто): новая единица {proposal.kb_id} «{proposal.title}» — "
                f"{draft.origin}. Класс {YELLOW}: новая тема [status: needs-check]"
            )
            async with self.lock:
                result = await asyncio.to_thread(
                    kb_write.apply_new_unit, self.kb, self.publisher, proposal, message
                )
                if result.wrote:
                    self.kb.reload()
                    # Кэш ответов сбрасываем целиком: ответ «в базе этого нет»
                    # ни на одну единицу не ссылается, а неверным стал именно он.
                    self.answer_cache.clear(self.kb.unit_hashes(), f"создана {proposal.kb_id}")
            if not result.wrote:
                return {"class": "human", "reason": f"новая единица не записалась: {result.message}"}
            summary = f"новая единица «{proposal.title}»"
        else:
            message = (
                f"База (бот): {proposal.summary} [{draft.kb_id}] — {draft.origin}, "
                f"формулировку подтвердил {who}"
                if draft.confirmed else
                f"База (авто): {proposal.summary} [{draft.kb_id}] — {draft.origin}. "
                f"Класс {draft.level}: {draft.reason}"
                + (" [status: needs-check — факт не подтверждён]" if draft.marked else "")
            )
            result = await self._write(proposal, message)
            if not result.wrote:
                return {"class": "human", "reason": f"запись не прошла: {result.message}"}
            summary = proposal.summary

        record = {
            "class": draft.level,
            "kb_id": draft.kb_id,
            "summary": summary,
            "reason": draft.reason,
            "origin": draft.origin,
            "fact": draft.fact,
            "sha": result.sha,
            "committed": result.ok,
            "evidence": draft.evidence,
            "confirmed": draft.confirmed,
        }
        # В журнал автоправок (недельный отчёт и суточный лимит) идёт только
        # записанное без человека; подтверждённое автором — обычная правка.
        if not draft.confirmed:
            self.state.note_auto_edit(record)
        log.info(
            "%s %s в %s: %s (%s)",
            "Запись с подтверждением" if draft.confirmed else "Автоправка",
            draft.level, draft.kb_id, summary, draft.reason,
        )
        return record

    async def age_claims(self) -> list[str]:
        """Снимает оговорку «не подтверждено» с записей, отлежавшихся без возражений
        (правило — `aging.py`). Один коммит на прогон. Возвращает id затронутых единиц."""
        changed: list[tuple[str, str]] = []  # (kb_id, относительный путь)
        async with self.lock:
            for unit in list(self.kb.units.values()):
                try:
                    text = unit.path.read_text(encoding="utf-8")
                except OSError:
                    continue
                new_text, aged = aging.age_text(text)
                if not aged:
                    continue
                kb_write.write_atomic(unit.path, new_text)
                rel = str(unit.path.relative_to(self.kb.root)).replace("\\", "/")
                changed.append((unit.id, rel))
            if not changed:
                return []
            ids = ", ".join(kb_id for kb_id, _ in changed)
            ok, note = await asyncio.to_thread(
                self.publisher.commit_paths, [rel for _, rel in changed],
                f"База (авто): сняты оговорки «не подтверждено» с записей старше "
                f"{aging.AGE_DAYS} дней без возражений — {ids}",
            )
            self.kb.reload()
            self.answer_cache.sync(self.kb.unit_hashes())
        log.info("Старение оговорок: %s (git: %s)", ids, "ok" if ok else note)
        return [kb_id for kb_id, _ in changed]

    async def _write(self, proposal, message: str):
        """Запись под общим локом — тем же, что у ручного пути."""
        async with self.lock:
            result = await asyncio.to_thread(
                kb_write.apply_edit, self.kb, self.publisher, proposal, message
            )
            if result.wrote:
                self.kb.reload()
                self.answer_cache.sync(self.kb.unit_hashes())
        return result

    async def rollback(self, sha: str, reason: str = "") -> tuple[bool, str]:
        """Откат автоправки по кнопке в отчёте."""
        async with self.lock:
            ok, note = await asyncio.to_thread(self.publisher.revert, sha, reason)
            if ok:
                self.kb.reload()
                self.answer_cache.sync(self.kb.unit_hashes())
        return ok, note


_WORD_TO_CLASS = {
    "зелёный": GREEN, "зеленый": GREEN, "green": GREEN,
    "жёлтый": YELLOW, "желтый": YELLOW, "yellow": YELLOW,
    "красный": RED, "red": RED,
}


def parse_risk(answer: str) -> tuple[str, str]:
    """Ответ модели → (класс, причина). Непонятное — RED."""
    lines = [ln.strip() for ln in (answer or "").splitlines() if ln.strip()]
    if not lines:
        return RED, "классификатор риска не ответил"
    first = lines[0].lower().strip(".:!— ")
    for word, level in _WORD_TO_CLASS.items():
        if first.startswith(word):
            return level, (lines[1] if len(lines) > 1 else "")
    return RED, f"не разобрал ответ классификатора: {lines[0][:50]}"
