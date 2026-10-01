"""Публикация в git: сырьё (библиотека файлов, поток рабочих чатов, снимок состояния)
уходит пачкой раз в сутки или по кнопке; единицы базы — по одной правке на коммит
(`commit_paths`), ради аудита.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import alerts
import botstate

log = logging.getLogger(__name__)

PUBLISH_EVERY = timedelta(days=1)
CHECK_EVERY = 3600  # раз в час сверяемся: не пора ли публиковать

# Что уходит пачкой: только сырьё; knowledge/ коммитится поштучно.
BATCH_PATHS = ["files", "chats-live", "bot-state"]

# Снимок состояния бота (роли, доступы, чаты, модель) в репозитории: при запуске
# на пустом томе бот восстанавливается из него (`restore_state`).
STATE_SNAPSHOT = "bot-state/state.json"
SNAPSHOT_KEYS = ("leaders", "leaders_env_seen", "extra_managers", "users", "chats", "model", "autonomy")


def restore_state(kb_root: Path, state_path: Path) -> bool:
    """Первый запуск на новом сервере: состояния нет — берём снимок из репозитория."""
    snapshot = kb_root / STATE_SNAPSHOT
    if state_path.exists() or not snapshot.is_file():
        return False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, state_path)
    log.warning("Состояние бота восстановлено из снимка в репозитории: %s", snapshot)
    return True


def _tracked(method):
    """Сообщает о сбоях записи в git модулю критичных сигналов (`alerts.py`)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        if result[0]:
            alerts.ok(alerts.GIT)
        else:
            alerts.fail(alerts.GIT, result[1])
        return result
    return wrapper


class Publisher:
    def __init__(self, repo_root: Path, state) -> None:
        self.repo = repo_root
        self.state = state  # хранит время последней публикации (переживает перезапуск)

    def _git(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        """Возвращает (код, вывод); исключения не пробрасывает — ошибка возвращается текстом."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.error("git %s не ответил за %s сек", args[0] if args else "?", timeout)
            return 1, f"git {args[0] if args else ''} завис (таймаут {timeout} сек)"
        except OSError as error:
            log.error("не смог запустить git: %s", error)
            return 1, f"не смог запустить git: {error}"
        return result.returncode, (result.stdout + result.stderr).strip()

    def has_pending(self) -> bool:
        """Незакоммиченные изменения сырья или закоммиченные, но не отправленные коммиты."""
        code, out = self._git("status", "--porcelain", "--", *BATCH_PATHS)
        if code == 0 and out.strip():
            return True
        return self._has_unpushed()

    def _has_unpushed(self) -> bool:
        code, out = self._git("rev-list", "--count", "@{u}..HEAD")
        try:
            return code == 0 and int(out.strip() or "0") > 0
        except ValueError:
            return False

    def _pull_rebase(self, timeout: int = 90) -> tuple[int, str]:
        """`git pull --rebase --autostash`: слушатель непрерывно пишет в chats-live/,
        и без autostash git отказывается ребейзить поверх грязного дерева."""
        return self._git("pull", "--rebase", "--autostash", "origin", "main", timeout=timeout)

    def _batch_paths(self) -> list[str]:
        """Пути пачки, которые существуют: `git add` с отсутствующим путём падает целиком."""
        return [p for p in BATCH_PATHS if (self.repo / p).exists()]

    def _snapshot_state(self) -> None:
        """Кладёт копию состояния бота в репозиторий — она уедет этим же коммитом."""
        source = getattr(self.state, "path", None)
        if source is None or not Path(source).is_file():
            return
        target = self.repo / STATE_SNAPSHOT
        try:
            # Только то, что нужно новому серверу; курсоры и журнал менялись бы каждый день.
            full = json.loads(Path(source).read_text(encoding="utf-8"))
            text = json.dumps({k: full[k] for k in SNAPSHOT_KEYS if k in full},
                              ensure_ascii=False, indent=2) + "\n"
            if target.is_file() and target.read_text(encoding="utf-8") == text:
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError:
            log.warning("Не смог обновить снимок состояния бота", exc_info=True)

    @_tracked
    def publish(self, reason: str = "суточная публикация") -> tuple[bool, str]:
        """Коммитит files/ и chats-live/ и пушит. Возвращает (успех, сообщение)."""
        self._snapshot_state()
        if not self.has_pending():
            self.state.mark_published()
            return True, "Нового нет — публиковать нечего."

        paths = self._batch_paths()
        code, out = self._git("add", "--", *paths)
        if code != 0:
            return False, f"git add не прошёл: {out}"

        # Если новых правок нет, но остались неотправленные коммиты — сразу к pull+push.
        count = self._pending_count()
        if count:
            # Пути и в commit: иначе закоммитится всё, что оказалось в индексе.
            code, out = self._git(
                "commit", "-m", f"Сырьё (бот): библиотека и поток рабочих чатов ({reason})",
                "--", *paths,
            )
            if code != 0:
                return False, f"git commit не прошёл: {out}"

        code, out = self._pull_rebase()
        if code != 0:
            log.error("git pull --rebase не прошёл: %s", out)
            return False, self._abort_rebase(out)

        code, out = self._git("push", "origin", "main", timeout=90)
        if code != 0:
            return False, f"git push не прошёл: {out}"

        self.state.mark_published()
        log.info("Опубликовано в git (%s)", reason)
        return True, (
            f"Опубликовано в git: {count} изменений (библиотека + поток чатов)."
            if count else "Дожал ранее застрявший коммит — всё теперь в git."
        )

    def _abort_rebase(self, reason: str) -> str:
        """Откатывает недоделанный rebase: иначе все последующие записи в git ломаются."""
        code, out = self._git("rebase", "--abort")
        if code == 0:
            log.warning("Незавершённый rebase откачен, репозиторий чистый")
            return f"не смог синхронизироваться с GitHub (rebase откачен): {reason}"
        # --abort падает и когда rebase не начинался — это нормально, не шумим.
        log.info("rebase --abort: %s", out)
        return f"не смог синхронизироваться с GitHub: {reason}"

    def _pending_count(self) -> int:
        code, out = self._git("diff", "--cached", "--name-only", "--", *BATCH_PATHS)
        return len([line for line in out.splitlines() if line.strip()]) if code == 0 else 0

    @_tracked
    def commit_paths(self, paths: list[str], message: str) -> tuple[bool, str]:
        """Коммитит и пушит конкретные пути (правку единицы базы). Синхронный I/O — звать через to_thread."""
        code, out = self._git("add", "--", *paths)
        if code != 0:
            return False, f"git add не прошёл: {out}"
        # Пути и в commit: иначе закоммитится всё, что было в индексе.
        code, out = self._git("commit", "-m", message, "--", *paths)
        if code != 0:
            return False, f"git commit не прошёл: {out}"
        code, out = self._pull_rebase()
        if code != 0:
            return False, self._abort_rebase(out)
        code, out = self._git("push", "origin", "main", timeout=90)
        if code != 0:
            return False, f"git push не прошёл: {out}"
        log.info("Закоммичено в git: %s (%s)", paths, message)
        return True, "готово"

    def head_sha(self) -> str:
        """Хэш последнего коммита — по нему откатывается автоправка из отчёта."""
        code, out = self._git("rev-parse", "HEAD")
        return out.strip() if code == 0 else ""

    def revert(self, sha: str, reason: str = "") -> tuple[bool, str]:
        """Откатывает коммит отдельным коммитом-обраткой (`revert`, не `reset`: история — аудит) и пушит."""
        if not re.fullmatch(r"[0-9a-f]{7,40}", (sha or "").strip()):
            return False, "не похоже на хэш коммита"
        note = f"Откат автоправки {sha[:8]}" + (f": {reason}" if reason else "")
        code, out = self._git("revert", "--no-edit", sha)
        if code != 0:
            # Незавершённый revert ломает всё последующее — прибираем за собой.
            self._git("revert", "--abort")
            return False, f"git revert не прошёл: {out}"
        code, out = self._git("commit", "--amend", "-m", note)
        if code != 0:
            log.warning("Не смог переписать сообщение отката: %s", out)
        code, out = self._pull_rebase()
        if code != 0:
            return False, self._abort_rebase(out)
        code, out = self._git("push", "origin", "main", timeout=90)
        if code != 0:
            return False, f"git push не прошёл: {out}"
        log.info("Откачена автоправка %s", sha[:8])
        return True, "откатил"

    async def run_periodic(self) -> None:
        """Фоновая задача: раз в час проверяет по отметке в state, прошли ли сутки с последней публикации."""
        while True:
            try:
                last = self.state.last_published
                due = last is None or (datetime.now(timezone.utc) - last) >= PUBLISH_EVERY
                if due and self.has_pending():
                    # git — блокирующий I/O, уводим в поток; под общим замком записи,
                    # чтобы не трогать индекс и rebase одновременно с другой правкой.
                    async with botstate.KB_WRITE_LOCK:
                        ok, msg = await asyncio.to_thread(self.publish)
                    log.info("Плановая публикация: %s", msg)
                elif due:
                    self.state.mark_published()  # нечего публиковать — сдвигаем отметку
            except Exception:
                log.exception("Сбой плановой публикации — попробую в следующий раз")
            await asyncio.sleep(CHECK_EVERY)
