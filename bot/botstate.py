"""Что бот помнит в пределах процесса: незавершённые действия, локи, номера.

Импортировать отсюда надо имена, а не копии: все объекты мутабельные и нигде не
переприсваиваются. Ничто здесь не переживает перезапуск — намеренно.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from kb_editor import EditProposal, NewUnitProposal
from uploads import PendingUpload

# --- Локи -------------------------------------------------------------------

# aiogram обрабатывает апдейты параллельно: без лока два быстрых вопроса одного
# менеджера мутировали бы общий Dialog вперемешку.
USER_LOCKS: dict[int, asyncio.Lock] = {}

# Записи в базу строго по одной: блок «сверить с диском → записать файл → git add,
# commit, pull --rebase, push → перечитать базу» целиком под одним локом, иначе
# две записи упираются в .git/index.lock или в середину чужого rebase.
# Лок процессный; от правок извне защищает сверка с диском перед записью.
KB_WRITE_LOCK = asyncio.Lock()


def user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_LOCKS.get(user_id)
    if lock is None:
        lock = USER_LOCKS[user_id] = asyncio.Lock()
    return lock


# --- Номера предложений -----------------------------------------------------

# Предложения адресуются номером из callback_data, а не user_id: кнопка должна
# применить то предложение, diff которого человек прочитал. Отсчёт от времени запуска,
# чтобы кнопка из сообщения до рестарта не попала в другое предложение.
_PENDING_SEQ = int(time.time())


def next_pending_id() -> int:
    global _PENDING_SEQ
    _PENDING_SEQ += 1
    return _PENDING_SEQ


def take_pending(store: dict, raw_key: str, user_id: int):
    """Забирает предложение по номеру из callback_data. None — если номер чужой,
    неизвестный или уже использован (кнопку нажали дважды)."""
    try:
        key = int(raw_key)
    except (TypeError, ValueError):
        return None
    row = store.get(key)
    if row is None or row[0] != user_id:
        return None
    del store[key]
    return row[1]


# --- Правка базы ------------------------------------------------------------

# Номер предложения → (кто подтверждает, предложение).
PENDING_EDITS: dict[int, tuple[int, EditProposal]] = {}

# Номер → (кто подтверждает, готовая новая единица).
PENDING_NEW_UNITS: dict[int, tuple[int, NewUnitProposal]] = {}

# Кто нажал «✏️ Обновить базу» и сейчас пишет текст правки.
PENDING_UPDATE: set[int] = set()

# Кто из менеджеров нажал «✏️ Предложить в базу» и сейчас пишет текст предложения.
PENDING_PROPOSAL: set[int] = set()

# Кто сейчас пишет предложение по работе бота (нажал «💡 Идея / пожелание»).
PENDING_SUGGESTION: set[int] = set()


@dataclass
class ManagerEditRequest:
    """Предложение правки от менеджера, ждущее решения руководителя; модель ещё не вызывалась."""
    request_id: int
    manager_id: int
    manager_name: str
    manager_username: str | None
    instruction: str
    # Если правка пришла из пересланного сообщения — откуда именно (для sources и коммита).
    origin: str | None = None


# Предложения правок от менеджеров, ждущие решения руководителя (по request_id).
# Номера общие с остальными предложениями (next_pending_id).
PENDING_MANAGER_EDITS: dict[int, ManagerEditRequest] = {}


@dataclass
class EditContext:
    """Откуда взялась правка — всё, что нужно показать человеку перед подтверждением."""
    instruction: str  # что именно сказал человек — цитата
    attribution: str | None = None  # кто предложил, если не руководитель
    attribution_id: int | None = None
    origin: str | None = None  # откуда текст (переслано из чата, автор, дата)
    # Почему правка ложится в эту единицу: ответ модели на шаге маршрутизации, по kb-id.
    reasons: dict[str, str] = field(default_factory=dict)
    # Пункт сводки, из которого выросла правка: закрывается по факту записи, а не нажатия.
    fact_id: str | None = None
    # Кто подтверждает правку: имя встанет подписью под записью.
    author: str = ""
    # От роли зависит только подача: полный diff или название темы и строки «+»/«−».
    leader: bool = False


# Номер → (кто подтверждает, контекст правки). Нужен на следующем шаге:
# выбор единицы или создание новой.
PENDING_CREATE_ASK: dict[int, tuple[int, EditContext]] = {}


# --- Файлы, ссылки, пересланное ---------------------------------------------

# Незавершённые загрузки: прислали файл, ждём описание.
PENDING_UPLOADS: dict[int, PendingUpload] = {}

# Руководитель правит карточку файла: ждём от него три строки описания.
PENDING_FILE_META: dict[int, str] = {}

# Прислали ссылку без пояснений — держим текст, пока человек не выберет, что это.
PENDING_LINK: dict[int, str] = {}


@dataclass
class PendingForward:
    """Пересланное в бота сообщение: ждём, что с ним сделать (в базу / идея / ничего)."""
    text: str
    origin: str


# Пересланные сообщения, ждущие решения «что с этим сделать» (по user_id).
PENDING_FORWARDS: dict[int, PendingForward] = {}


# --- Доступ -----------------------------------------------------------------

# Заявки на доступ: id → (имя, ник). И те, о ком уже спросили руководителя — чтобы
# не дёргать его на каждое сообщение одного и того же человека.
PENDING_ACCESS: dict[int, tuple[str, str]] = {}
ACCESS_ASKED: set[int] = set()


# --- Ответы на вопросы ------------------------------------------------------

# Вопрос по id сообщения-ответа: (chat_id, message_id) → (вопрос, модель).
# Ключ с chat_id: id сообщения уникален только внутри чата. Модель рядом: кэш
# разложен по моделям, а её могли переключить между ответом и оценкой.
QUESTION_BY_MSG: dict[tuple[int, int], tuple[str, str]] = {}


@dataclass
class MoreRequest:
    """Что разворачивать по кнопке «Подробнее»; живёт в памяти процесса."""
    question: str
    units: list[str]
    model: str
    in_group: bool


# Номер запроса → что разворачивать. Номер лежит в callback_data (в неё не влезет
# ни вопрос, ни список единиц: у Telegram лимит 64 байта).
MORE_PENDING: dict[int, MoreRequest] = {}


# --- Меню: ввод по запросу кнопки --------------------------------------------

# Кто нажал в меню кнопку, после которой бот ждёт текст: `add_manager` — ник или id
# нового менеджера, `add_leader` — id нового руководителя, `compare` — вопрос для
# сравнения моделей. user_id → вид ввода. Разбирает `h_menu.on_menu_input`.
PENDING_MENU_INPUT: dict[int, str] = {}


# --- Запись из рабочего чата: черновик ждёт «верно» от автора ------------------

# Номер → (id автора, черновик `autonomy.Draft`, время создания по time.time()).
# В базу черновик попадёт только после нажатия автора.
PENDING_CHAT_DRAFTS: dict[int, tuple[int, object, float]] = {}
# Дольше — контекст забыт, а текст единицы мог поменяться.
CHAT_DRAFT_TTL = 24 * 3600


# --- Общие мелочи режимов ввода ---------------------------------------------


def forget_pending_text(user_id: int) -> bool:
    """Сбрасывает все режимы ожидания текста. True — если что-то ждали."""
    was = any(user_id in waiting for waiting in (PENDING_UPDATE, PENDING_PROPOSAL, PENDING_SUGGESTION))
    for waiting in (PENDING_UPDATE, PENDING_PROPOSAL, PENDING_SUGGESTION):
        waiting.discard(user_id)
    if PENDING_MENU_INPUT.pop(user_id, None) is not None:
        was = True
    return was


def is_cancel(text: str) -> bool:
    """«Отмена» в любом виде; один список на все режимы ожидания."""
    return text.strip().lower().rstrip(".!") in {"отмена", "отменить", "cancel", "/cancel"}
