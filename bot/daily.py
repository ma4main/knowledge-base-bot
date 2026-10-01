"""Ночной разбор рабочих чатов: факты из потока сверяются с базой, безопасное
записывается автоматически, остальное ждёт решения человека. Здесь же недельный
отчёт о том, что бот записал сам.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import dedupe
import scrub
from datetime import date, datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from formatting import esc_html as _esc

log = logging.getLogger(__name__)

# Часы запуска разбора (время контейнера). Окно прогона — от предыдущего до текущего.
DIGEST_HOURS = (21,)

# Свежий хвост потока (минут) в разбор не берём: тред мог ещё не закончиться.
QUIET_MINUTES = 45

# Недельный отчёт: день недели (0 = понедельник) и час.
WEEKLY_DAY = 0
WEEKLY_HOUR = 10

# Час напоминания о гипотезах с прошедшей контрольной точкой.
HYPOTHESIS_HOUR = 10

# Сколько дней пункт разбора ждёт решения, прежде чем закрыться сам.
QUEUE_DAYS = 14

# Период проверки расписания, секунд.
CHECK_EVERY = 10 * 60

# Границы одного прогона: чатов, сообщений на чат, знаков на чат и фактов на сверку.
MAX_CHATS = 8
MAX_MESSAGES = 600
MAX_CHARS = 60000
MAX_FACTS_PER_CHAT = 6
MAX_FACTS_TOTAL = 10

_SKIP = {"уже есть"}


class DailyDigest:
    def __init__(
        self, bot, config, state, chat_log, kb_editor, notify, kb=None, auto=None
    ) -> None:
        self.bot = bot
        self.config = config
        self.state = state
        self.chat_log = chat_log
        self.kb_editor = kb_editor
        self._notify = notify
        self.kb = kb
        self.auto = auto

    async def run_periodic(self) -> None:
        while True:
            try:
                if self._due():
                    await self.send()
                if self._weekly_due():
                    await self.send_weekly()
            except Exception:
                log.exception("Сбой разбора чатов — попробую в следующий раз")
            await asyncio.sleep(CHECK_EVERY)

    def _weekly_due(self) -> bool:
        """Пора ли слать недельный отчёт; пропущенный день догоняется при первой возможности."""
        now = datetime.now()
        today = now.date().isoformat()
        if self.state.last_weekly_at == today:
            return False
        if now.weekday() == WEEKLY_DAY and now.hour >= WEEKLY_HOUR:
            return True
        last = self.state.last_weekly_at
        if not last:
            return now.hour >= WEEKLY_HOUR
        try:
            missed = (now.date() - date.fromisoformat(last)).days > 7
        except ValueError:
            return True
        return missed and now.hour >= WEEKLY_HOUR

    async def remind_hypotheses(self) -> bool:
        """Напоминание о просроченных гипотезах и пунктах без решения; True — если ушло.
        По расписанию не вызывается."""
        if self.kb is None:
            return False
        now = datetime.now()
        if now.hour < HYPOTHESIS_HOUR:
            return False
        today = now.date().isoformat()
        if self.state.last_hypothesis_reminder == today:
            return False
        overdue = [u for u in self.kb.open_experiments(today) if u.control_point <= today]
        pending = self.state.pending_digest_facts()
        # Отметку ставим в любом случае, иначе проверка повторялась бы до конца суток.
        self.state.mark_hypothesis_reminder()
        if not overdue and not pending:
            return False

        lines: list[str] = []
        if overdue:
            lines.append("⏳ <b>Гипотезы, по которым пора подвести итог</b>")
            lines.append("")
            for unit in overdue:
                lines.append(
                    f"• <b>{unit.id}</b> · {_esc(unit.title[:70])}\n"
                    f"   контрольная точка была {unit.control_point}"
                )
            lines.append("")
            lines.append(
                "<i>Закрыть — обычной правкой: «обнови базу: по kb-104 итог такой-то». "
                "Если рано — скажи новую дату, поправлю контрольную точку.</i>"
            )
        if pending:
            if lines:
                lines.append("")
            lines.append(
                f"📥 <b>Ждут решения: {len(pending)} пунктов из сводок</b> — "
                f"бот услышал их в чатах, но в базу не записал."
            )
            for _fid, fact in pending[:3]:
                lines.append(f"• {_esc((fact.get('text') or '')[:90])}")
            if len(pending) > 3:
                lines.append(f"• …и ещё {len(pending) - 3}")
            lines.append("<i>Разобрать — /hvosty</i>")
        lines.append("")
        lines.append("<i>Пока не разобрано — напоминаю раз в день.</i>")

        await self._notify(self.bot, self.config, "\n".join(lines), None)
        log.info(
            "Напоминание: гипотез %d (%s), пунктов сводок %d",
            len(overdue), ", ".join(u.id for u in overdue) or "нет", len(pending),
        )
        return True

    def _due(self) -> bool:
        """Наступил ли слот из DIGEST_HOURS, в котором прогона ещё не было."""
        now = datetime.now()
        slots = [h for h in DIGEST_HOURS if h <= now.hour]
        if not slots:
            return False
        slot_start = now.replace(hour=slots[-1], minute=0, second=0, microsecond=0)
        # По отметке запуска, а не по курсору потока: курсор остаётся на месте
        # после сбоя, и прогон повторялся бы каждую проверку.
        last = self.state.last_digest_run or self.state.last_digest_at
        if not last:
            return True
        try:
            # last_digest_at пишется в UTC — сравниваем в местном времени контейнера.
            last_local = datetime.fromisoformat(last).astimezone().replace(tzinfo=None)
        except ValueError:
            return True
        return last_local < slot_start

    def _checkpoint(self, until: str, failed: int) -> None:
        """Двигает курсор потока, только если окно разобрано без сбоев."""
        if failed:
            log.warning(
                "Курсор потока НЕ двигаю: не разобрано чатов %d — окно перечитаем "
                "в следующий прогон", failed,
            )
            return
        self.state.mark_digest(until)

    async def send(self) -> None:
        """Разбирает окно с прошлого прогона. Молчит, если пусто."""
        expired = self.state.expire_digest_facts(QUEUE_DAYS)
        if expired:
            log.info("Очередь на решение: закрыто по сроку %d пунктов", expired)
        if self.auto is not None:
            try:
                await self.auto.age_claims()
            except Exception:
                log.exception("Старение оговорок не удалось — попробую в следующий прогон")
        chats = {k: v for k, v in self.state.chats.items() if not v.get("muted")}
        since = self.state.last_digest_at
        # Окна ещё нет — берём последние сутки.
        if not since:
            since = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        # Границу разобранного ставим по обрезке, а не по «сейчас», иначе хвост потеряется.
        until = (
            datetime.now(timezone.utc) - timedelta(minutes=QUIET_MINUTES)
        ).isoformat(timespec="seconds")
        if until <= since:
            return
        # Отметку запуска ставим сразу, курсор потока — в самом конце, когда
        # результаты уже сохранены.
        self.state.mark_digest_run()
        if not chats:
            self.state.mark_digest(until)
            return

        facts: list[dict] = []
        noise_total = 0
        chats_seen = 0
        failed = 0
        listening = list(chats.items())
        if len(listening) > MAX_CHATS:
            log.warning(
                "Разбор чатов: слушаем %d, за прогон беру %d — остальные в этом окне "
                "не разбираются", len(listening), MAX_CHATS,
            )
        for raw_id, meta in listening[:MAX_CHATS]:
            stream = await asyncio.to_thread(
                self.chat_log.as_text, int(raw_id), MAX_MESSAGES, since, until
            )
            if not stream.strip():
                continue
            chats_seen += 1
            if len(stream) > MAX_CHARS:
                log.warning(
                    "Разбор чатов: поток «%s» %d знаков, беру последние %d — начало "
                    "окна в разбор не попадёт", meta.get("title") or raw_id,
                    len(stream), MAX_CHARS,
                )
                stream = stream[-MAX_CHARS:].split("\n", 1)[-1]
            title = meta.get("title") or raw_id
            stream, hidden = scrub.clean(stream)
            if hidden:
                log.info(
                    "Вычистка «%s»: скрыто фрагментов с контактами и доменами: %d",
                    title, hidden,
                )
            try:
                found, noise = await self.kb_editor.extract_facts(
                    title, stream, limit=MAX_FACTS_PER_CHAT
                )
            except Exception:
                failed += 1
                log.exception("Не разобрал поток чата «%s» — окно перечитаю", title)
                continue
            noise_total += noise
            # chat_id нужен провенансу: по нему и msg_id достаётся настоящий автор.
            for fact in found:
                fact["chat_id"] = int(raw_id)
            facts.extend(found)

        if not facts:
            log.info(
                "Утренняя сводка: за сутки знания не нашлось (чатов %d, операционки %d) — молчу",
                chats_seen, noise_total,
            )
            self._checkpoint(until, failed)
            return

        assessed = await asyncio.gather(
            *(self.kb_editor.assess_fact(f["text"]) for f in facts[:MAX_FACTS_TOTAL]),
            return_exceptions=True,
        )
        rows = []
        for fact, result in zip(facts, assessed):
            if isinstance(result, Exception):
                log.warning("Не смог сверить факт с базой: %s", fact["text"][:80])
                continue
            verdict, kb_id, note = result
            if verdict in _SKIP:
                continue
            rows.append({**fact, "verdict": verdict, "kb_id": kb_id, "note": note})

        if not rows:
            log.info("Сводка: всё найденное уже есть в базе — молчу")
            self._checkpoint(until, failed)
            return

        rows, repeated = self._drop_duplicates(rows)
        if not rows and not repeated:
            log.info("Сводка: всё найденное — повторы уже разобранного, молчу")
            self._checkpoint(until, failed)
            return

        # Сначала дедупликация, потом автозапись — иначе один факт из двух чатов
        # запишется дважды.
        applied: list[dict] = []
        if self.auto is not None and self.state.autonomy:
            kept = []
            for row in rows:
                who = row.get("who") or ""
                msg_id = row.get("msg_id") or ""
                origin = f"сводка по чатам, {row.get('chat') or 'рабочий чат'}"
                if who:
                    origin += f", со слов {who}"
                if msg_id:
                    origin += f", сообщение #{msg_id}"
                # Провенанс: автора и время берём из записанного потока, а не из
                # пересказа модели.
                src = None
                if msg_id and row.get("chat_id"):
                    try:
                        src = await asyncio.to_thread(
                            self.chat_log.find, int(row["chat_id"]), int(msg_id)
                        )
                    except (TypeError, ValueError):
                        src = None
                prov = {
                    "who": (src.user_name if src else "") or who,
                    "who_id": src.user_id if src else None,
                    "msg": f"{row.get('chat') or ''}#{msg_id}" if msg_id else "",
                    "said": src.at if src else "",
                }
                # Противоречие автомат не разрешает: пункт остаётся в очереди.
                if row.get("verdict") == "противоречит":
                    row["auto_note"] = "противоречит записанному — автомат не выбирает сам"
                    kept.append(row)
                    continue
                try:
                    verdict = await self.auto.consider(row["text"], origin, prov=prov)
                except Exception:
                    log.exception("Автоправка не удалась, оставляю человеку: %s", row["text"][:70])
                    verdict = {"class": "human", "reason": "сбой автоправки"}
                # `red` тоже записан (с needs-check): показать его как «ждёт решения»
                # значило бы получить дубль при следующем нажатии.
                if verdict.get("class") in {"green", "yellow", "red"}:
                    applied.append(verdict)
                else:
                    row["auto_note"] = verdict.get("reason", "")
                    kept.append(row)
            rows = kept

        # Короткий id: текст факта в 64 байта callback_data не влезет.
        for i, row in enumerate(rows, 1):
            row["n"] = i
            row["fid"] = uuid.uuid4().hex[:8]
            # Пункты без темы в очередь не кладём: решить по ним может только
            # человек, знающий контекст; сообщение остаётся в потоке чата.
            if (row.get("verdict") or "").startswith("не понял"):
                log.info("Пункт без темы в очередь не кладу: %s", row["text"][:70])
                continue
            self.state.remember_digest_fact(
                row["fid"], row["text"], row.get("chat", ""), row.get("verdict", "")
            )

        log.info(
            "Разбор чатов: записано сам %d, осталось человеку %d, из %d чатов "
            "(операционки %d, повторов %d)",
            len(applied), len(rows), chats_seen, noise_total, len(repeated),
        )
        self._checkpoint(until, failed)

    async def send_weekly(self) -> None:
        """Недельный отчёт руководителям: одно сообщение, ответа не требует."""
        self.state.mark_weekly()
        since = (date.today() - timedelta(days=7)).isoformat()
        written = self.state.auto_edits_since(since)
        text = weekly_text(
            written=written,
            conflicts=[
                fact.get("text", "") for _fid, fact in self.state.pending_digest_facts()
                if (fact.get("verdict") or "").startswith("противоречит")
                and (fact.get("at") or "")[:10] >= since
            ],
            overdue=(
                [u for u in self.kb.open_experiments(date.today().isoformat())
                 if u.control_point <= date.today().isoformat()]
                if self.kb is not None else []
            ),
            needs_check=(
                sum(1 for u in self.kb.units.values() if getattr(u, "status", "") == "needs-check")
                if self.kb is not None else 0
            ),
            titles={uid: u.title for uid, u in self.kb.units.items()} if self.kb is not None else {},
        )
        await self._notify(
            self.bot, self.config, text, _undo_keyboard(written[:WEEKLY_MAX_ROWS])
        )
        log.info("Недельный отчёт отправлен: автоправок за неделю %d", len(written))

    def _drop_duplicates(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        """Схлопывает повторы; возвращает (новое, повторы). Порядок сверки: с открытыми
        пунктами (им наращиваем счётчик), с уже разобранными (молчим), друг с другом."""
        open_texts = self.state.digest_texts(only_open=True)
        all_texts = self.state.digest_texts()
        settled = {fid: text for fid, text in all_texts.items() if fid not in open_texts}

        fresh: list[dict] = []
        repeated: list[dict] = []
        seen_now: dict[str, dict] = {}
        for row in rows:
            text = row["text"]
            hit = dedupe.find_duplicate(text, open_texts)
            if hit:
                fact = self.state.bump_digest_fact(hit[0], row.get("chat", ""))
                if fact is not None:
                    repeated.append({**row, "repeats": fact.get("repeats", 2)})
                    log.info("Повтор пункта сводки (%.2f): %s", hit[1], text[:70])
                continue
            if dedupe.find_duplicate(text, settled):
                log.info("Пункт уже разобран раньше — пропускаю: %s", text[:70])
                continue
            same_now = dedupe.find_duplicate(text, {k: k for k in seen_now})
            if same_now:
                twin = seen_now[same_now[0]]
                twin["also_chats"] = sorted(
                    set(twin.get("also_chats", [])) | {row.get("chat", "")}
                )
                log.info("Дубль внутри одной сводки: %s", text[:70])
                continue
            seen_now[text] = row
            fresh.append(row)
        return fresh, repeated


# Сколько автоправок показывать в недельном отчёте поимённо (и с кнопкой отката);
# остальное — числом.
WEEKLY_MAX_ROWS = 6
WEEKLY_MAX_CONFLICTS = 3


def weekly_text(
    written: list[dict], conflicts: list[str], overdue: list, needs_check: int,
    titles: dict[str, str] | None = None,
) -> str:
    """Текст недельного отчёта; чистая функция."""
    titles = titles or {}
    marks = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    lines = ["🤖 <b>Неделя в базе знаний</b>", ""]
    if written:
        counts = {level: sum(1 for r in written if r.get("class") == level) for level in marks}
        split = " ".join(f"{marks[k]}{v}" for k, v in counts.items() if v)
        lines.append(f"<b>Записал сам из рабочих чатов: {len(written)}</b> ({split})")
        for row in written[:WEEKLY_MAX_ROWS]:
            kb_id = row.get("kb_id", "")
            where = titles.get(kb_id) or kb_id
            lines.append(
                f"{marks.get(row.get('class'), '•')} {_esc(_short(row.get('summary', ''), 90))}"
                f" → <i>{_esc(_short(where, 50))}</i>"
            )
        if len(written) > WEEKLY_MAX_ROWS:
            lines.append(f"…и ещё {len(written) - WEEKLY_MAX_ROWS}. Все видны в «📅 Что изменилось».")
        lines.append("<i>🔴 — цифры и слова клиенту: записаны с пометкой «не подтверждено».</i>")
    else:
        lines.append("<b>Сам из рабочих чатов ничего не записал</b> — нового не было или всё уже есть.")
    if conflicts:
        lines += ["", f"⚠️ <b>Услышал то, что расходится с базой: {len(conflicts)}</b> — сам не выбирал:"]
        lines += [f"• {_esc(_short(text, 110))}" for text in conflicts[:WEEKLY_MAX_CONFLICTS]]
        if len(conflicts) > WEEKLY_MAX_CONFLICTS:
            lines.append(f"…и ещё {len(conflicts) - WEEKLY_MAX_CONFLICTS}.")
        lines.append("<i>Если верно новое — напиши мне, как правильно, покажу формулировку.</i>")
    if overdue:
        names = "; ".join(_short(u.title, 45) for u in overdue[:3])
        more = f" и ещё {len(overdue) - 3}" if len(overdue) > 3 else ""
        lines += ["", f"⏳ <b>Гипотезы без итога: {len(overdue)}</b> — {_esc(names)}{more}."]
    if needs_check:
        lines += ["", f"🟠 Тем с пометкой «не проверено»: {needs_check}. По ним отвечаю с оговоркой."]
    lines += [
        "",
        "<i>Отвечать на этот отчёт не нужно. Увидел ошибку — «↩️» под сообщением "
        "откатит запись, либо напиши мне, как правильно.</i>",
    ]
    return "\n".join(lines)


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _undo_keyboard(written: list[dict]) -> InlineKeyboardMarkup | None:
    """Кнопки отката под отчётом; правка без хэша (push сорвался) кнопки не получает."""
    buttons = [
        InlineKeyboardButton(
            text=f"↩️ {item.get('kb_id', '?')}", callback_data=f"undo:{item['sha'][:12]}"
        )
        for item in written if item.get("sha")
    ]
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    )


def _keyboard(rows: list[dict], applied: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Кнопки «Внести N» по пунктам и «Откатить N» по автоправкам с хэшем коммита."""
    buttons = [
        InlineKeyboardButton(text=f"✅ Внести {r['n']}", callback_data=f"dg:{r['fid']}")
        for r in rows
    ]
    for i, item in enumerate(applied or [], 1):
        if item.get("sha"):
            buttons.append(
                InlineKeyboardButton(
                    text=f"↩️ Откатить {i}", callback_data=f"undo:{item['sha'][:12]}"
                )
            )
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    )


def _render(
    rows: list[dict], noise: int, chats: int,
    repeated: list[dict] | None = None, applied: list[dict] | None = None,
) -> str:
    """Текст сводки; номера пунктов совпадают с кнопками под сообщением."""
    now = datetime.now()
    # Ряды могут прийти без номеров (так зовёт самопроверка).
    for i, row in enumerate(rows, 1):
        row.setdefault("n", i)
    add = [r for r in rows if r["verdict"] in {"новая тема", "уточняет"}]
    conflict = [r for r in rows if r["verdict"] == "противоречит"]
    unclear = [r for r in rows if r["verdict"] == "не понял"]

    out = [f"🗒 <b>Сводка по чатам — {now.strftime('%d.%m, %H:%M')}</b>", ""]

    if applied:
        out.append(f"🤖 <b>Внёс сам ({len(applied)}) — проверь:</b>")
        for i, item in enumerate(applied, 1):
            # Красный класс — записано с пометкой needs-check, а не отклонено.
            mark = {"green": "🟢", "yellow": "🟡"}.get(item.get("class"), "🔴")
            out.append(
                f"<b>{i}.</b> {mark} {_esc(item.get('summary', ''))} → {item.get('kb_id', '')}\n"
                f"   <i>{_esc(item.get('reason', ''))}</i>\n"
                f"   {_esc((item.get('fact') or '')[:130])}"
                + ("\n   ⚠️ помечено needs-check — отвечаю по нему с оговоркой"
                   if item.get("class") == "red" else "")
                + ("" if item.get("committed") else "\n   ⚠️ записал на диск, но в git не уехало")
            )
        out.append("<i>Не согласен — «↩️ Откатить N», верну как было отдельным коммитом.</i>")
        out.append("")

    if conflict:
        out.append("⚠️ <b>Расходится с базой — нужно решить, что верно:</b>")
        for r in conflict:
            out.append(
                f"<b>{r['n']}.</b> {_esc(r['text'])}\n"
                f"   <i>{_esc(r['who'])}, {_esc(r['chat'])}</i> → расходится с {r['kb_id']}"
                + (f": {_esc(r['note'])}" if r["note"] else "")
            )
        out.append("")

    if add:
        out.append("📌 <b>Похоже, стоит внести в базу:</b>")
        for r in add:
            where = (
                "новая тема" if r["verdict"] == "новая тема" else f"дополнит {r['kb_id']}"
            )
            out.append(
                f"<b>{r['n']}.</b> {_esc(r['text'])}\n"
                f"   <i>{_esc(r['who'])}, {_esc(r['chat'])}</i> → {where}"
            )
        out.append("")

    if unclear:
        out.append("❓ <b>Сам не разобрался — посмотри:</b>")
        for r in unclear:
            out.append(
                f"<b>{r['n']}.</b> {_esc(r['text'])}\n"
                f"   <i>{_esc(r['who'])}, {_esc(r['chat'])}</i>"
            )
        out.append("")

    # У повторов кнопки нет: они уже висят в хвостах со своей, вторая дала бы
    # две правки об одном и том же.
    if repeated:
        out.append("🔁 <b>Повторили то, что уже ждёт решения:</b>")
        for r in repeated:
            out.append(
                f"• {_esc(r['text'][:120])}\n"
                f"   <i>{_esc(r.get('chat', ''))}</i> → упоминание {r.get('repeats', 2)}-е, "
                f"решения по нему всё ещё нет"
            )
        out.append("<i>Разобрать — /hvosty, там повторяющиеся идут первыми.</i>")
        out.append("")

    out.append(
        f"<i>Прочитал {chats} чат(ов), операционку пропустил: {noise} сообщений.</i>"
    )
    out.append(
        "Кнопка «✅ Внести N» запускает обычный путь правки: подберу единицу, покажу "
        "diff и спрошу подтверждение. Сама сводка в базу ничего не пишет."
    )
    return "\n".join(out)
