"""Тексты и кнопки меню: подписи, клавиатуры, дерево меню и разбор нажатий по подписи.

Дерево описано данными (`TREE`), действия — в `h_menu.py`; право на пункт проверяется
и при отрисовке, и при нажатии.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

import kbconfig
from files_lib import FileLibrary
from links import LinkBook

LEADER = "leader"

GREETING = (
    f"Это база знаний компании «{kbconfig.CFG.company}».\n\n"
    "Спрашивай своими словами — например «у клиента просели показатели, что делать» "
    f"или «{kbconfig.CFG.example('question')}». Отвечаю только по базе: чего в ней нет, того "
    "не придумываю.\n\n"
    "Отвечаю коротко и по-разному, смотря что спросили: на вопрос про цифру или "
    "срок — пара строк (нужны детали — кнопка «📖 Подробнее» под ответом); "
    "на возражение клиента — суть и готовая фраза, которую можно сразу отправить; "
    "на сложную ситуацию сначала задам пару уточняющих вопросов, чтобы дать "
    "нужный вариант, а не всю выкладку.\n\n"
    "Всё остальное — по кнопке «☰ Меню» внизу или команде /menu."
)

MANAGER_HELP = (
    "\n\n<b>Что ещё умею</b>\n"
    "✏️ <b>Внести в базу.</b> Нашёл неточность или знаешь новое — напиши своими "
    "словами («запомни: минимальный срок договора теперь три месяца») или нажми «✏️ Внести в базу». Покажу, "
    "как именно запишу, и спрошу «верно?». После твоего «да» запись ложится в базу "
    "с твоим именем и датой.\n"
    f"📎 <b>Полезное.</b> Файлы и рабочие ссылки: «{kbconfig.CFG.example('file')}», "
    f"«{kbconfig.CFG.example('link')}». Ссылку можно и добавить: «запомни ссылку https://… — зум "
    "для планёрок».\n"
    "📅 <b>Что изменилось.</b> Сводка правок базы за 7 или 14 дней — удобно после "
    "отпуска.\n"
    "💡 <b>Идея.</b> Пожелание по работе бота — передам руководителю."
)

LEADER_HELP = MANAGER_HELP + (
    "\n\n<b>Руководителю</b>\n"
    "👥 <b>Люди и доступ</b> — кто пользуется ботом, выдать или забрать доступ, "
    "назначить или снять руководителя — выбором из списка людей. Заявки на доступ "
    "приходят тебе сами, с кнопками.\n"
    "⚙️ <b>Управление</b> — какие чаты записываю, оценки и идеи менеджеров, модель "
    "и расход, автозапись из чатов.\n"
    "Раз в неделю, в понедельник утром, присылаю отчёт: что записал в базу сам."
)
# Псевдоним: на него ссылаются другие модули.
ADMIN_HELP = LEADER_HELP

DENIED = (
    f"Привет! Это бот базы знаний компании «{kbconfig.CFG.company}» — отвечает на вопросы "
    "по продукту, работе с клиентами и внутренним правилам.\n\n"
    "Пока у тебя нет доступа. Заявку руководителю я уже отправил — как только "
    "её одобрят, напишу сюда."
)

# --- Постоянная клавиатура ---------------------------------------------------

BTN_MENU = "☰ Меню"

# Подписи снятой reply-клавиатуры: Telegram держит её на стороне клиента, пока бот
# не пришлёт новую, и нажатия обязаны работать, а не уезжать в модель как вопрос.
BTN_HELP = "❓ Что я умею"
BTN_FILES = "📎 Полезное (ссылки и файлы)"
BTN_SUGGEST = "💡 Идея / пожелание"
BTN_UPDATE = "✏️ Обновить базу"
BTN_PROPOSE = "✏️ Предложить в базу"
BTN_USERS = "👥 Люди"
BTN_PENDING = "📥 Разобрать хвосты"
BTN_HYPOTHESES = "⏳ Гипотезы"
BTN_CHANGES = "📅 Что изменилось"
# Ещё более старые подписи → их замены.
LEGACY_LABELS = {
    "📎 Файлы и презентации": BTN_FILES,
    "💡 Предложение": BTN_SUGGEST,
    "✏️ Предложить правку": BTN_PROPOSE,
}
MENU_LABELS = {
    BTN_MENU, BTN_HELP, BTN_FILES, BTN_SUGGEST, BTN_UPDATE, BTN_PROPOSE, BTN_USERS,
    BTN_PENDING, BTN_HYPOTHESES, BTN_CHANGES,
} | set(LEGACY_LABELS)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_MENU)]],
    resize_keyboard=True,
    input_field_placeholder="Спроси своими словами…",
)
# Псевдонимы: клавиатура одна на обе роли, различается само меню.
MENU_KEYBOARD = MAIN_KEYBOARD
ADMIN_KEYBOARD = MAIN_KEYBOARD


def menu_for(role: str) -> ReplyKeyboardMarkup:
    return MAIN_KEYBOARD


# --- Дерево меню -------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """Пункт меню. `target` — либо `nav:<экран>`, либо `do:<действие>`."""
    label: str
    target: str
    leader_only: bool = False


@dataclass(frozen=True)
class Screen:
    title: str
    # Строки кнопок: список рядов, в ряду один-два пункта.
    rows: tuple[tuple[Item, ...], ...]
    parent: str | None = None
    leader_only: bool = False


TREE: dict[str, Screen] = {
    "root": Screen(
        title="<b>Меню</b>\nВопрос по базе можно задать в любой момент — просто напиши его.",
        rows=(
            (Item("📚 База", "nav:base"), Item("✏️ Внести в базу", "do:update")),
            (Item("📎 Полезное", "nav:useful"), Item("💡 Идея / пожелание", "do:idea")),
            (Item("👥 Люди и доступ", "do:people", True), Item("⚙️ Управление", "nav:manage", True)),
            (Item("❓ Помощь", "nav:help"),),
        ),
    ),
    "base": Screen(
        title="<b>📚 База</b>\nЧто в базе действует, что менялось и что проверяется.",
        parent="root",
        rows=(
            (Item("📅 Что изменилось за 7 дней", "do:changes"),),
            (Item("🗺 Что действует сейчас", "do:state"),),
            (Item("📓 Журнал: что ломалось", "do:journal"),),
            (Item("⏳ Гипотезы без итога", "do:hypotheses"),),
        ),
    ),
    "useful": Screen(
        title="<b>📎 Полезное</b>\nФайлы и рабочие ссылки. Забрать можно и словами: "
              f"«{kbconfig.CFG.example('file')}», «{kbconfig.CFG.example('link')}».",
        parent="root",
        rows=(
            (Item("🔗 Ссылки", "do:links"), Item("📁 Файлы", "do:files")),
            (Item("➕ Как добавить", "do:howto_add"),),
        ),
    ),
    "help": Screen(
        title="<b>❓ Помощь</b>",
        parent="root",
        rows=(
            (Item("Что я умею", "do:help"),),
            (Item("Мой Telegram ID и роль", "do:whoami"),),
            (Item("Начать разговор заново", "do:reset"),),
        ),
    ),
    "manage": Screen(
        title="<b>⚙️ Управление</b>",
        parent="root",
        leader_only=True,
        rows=(
            (Item("💬 Чаты", "nav:chats", True), Item("📊 Качество", "nav:quality", True)),
            (Item("🤖 Бот", "nav:botcfg", True), Item("📥 Очередь на решение", "do:pending", True)),
        ),
    ),
    "chats": Screen(
        title="<b>💬 Чаты</b>\nЧто я записываю из рабочих чатов. Клиентские чаты "
              "записывать нельзя — выключи запись, если такой попал в список.",
        parent="manage",
        leader_only=True,
        rows=(
            (Item("Что записываю: включить / выключить", "do:chats", True),),
            (Item("Сводка услышанного", "do:svodka", True),),
            (Item("Отправить сырьё в git сейчас", "do:publish", True),),
        ),
    ),
    "quality": Screen(
        title="<b>📊 Качество</b>\nКак менеджеры оценивают ответы и о чём спрашивают.",
        parent="manage",
        leader_only=True,
        rows=(
            (Item("Оценки ответов", "do:otzyvy", True), Item("Идеи менеджеров", "do:idei", True)),
            (Item("О чём чаще всего спрашивают", "do:voprosy", True),),
        ),
    ),
    "botcfg": Screen(
        title="<b>🤖 Бот</b>\nМодель, расход и автозапись из рабочих чатов.",
        parent="manage",
        leader_only=True,
        rows=(
            (Item("Модель", "do:model", True), Item("Расход", "do:stats", True)),
            (Item("Сравнить модели на вопросе", "do:compare", True),),
            (Item("Автозапись из чатов: вкл / выкл", "do:autonomy", True),),
        ),
    ),
}

BACK = "← Назад"


def allowed(screen_or_item, role: str | None) -> bool:
    return bool(role) and (not screen_or_item.leader_only or role == LEADER)


def screen_markup(name: str, role: str | None) -> InlineKeyboardMarkup:
    """Кнопки экрана для роли: чужие пункты не рисуем вовсе."""
    screen = TREE[name]
    rows: list[list[InlineKeyboardButton]] = []
    for row in screen.rows:
        buttons = [
            InlineKeyboardButton(text=item.label, callback_data=f"m:{item.target}")
            for item in row if allowed(item, role)
        ]
        if buttons:
            rows.append(buttons)
    if screen.parent:
        rows.append([InlineKeyboardButton(text=BACK, callback_data=f"m:nav:{screen.parent}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def find_item(target: str) -> Item | None:
    """Пункт по его `target` — чтобы при нажатии проверить право на действие."""
    for screen in TREE.values():
        for row in screen.rows:
            for item in row:
                if item.target == target:
                    return item
    return None


# Явные текстовые зачины правки базы — можно без команды и без кнопки.
UPDATE_PREFIX = re.compile(r"^\s*(обнови(ть)?\s+баз[ауы]|внеси\s+в\s+баз[ауы]|запиши\s+в\s+баз[ауы]|поправь\s+в?\s*баз[ауы]|исправь\s+в?\s*баз[ауы])\b[:\-\s]*", re.IGNORECASE)

# Зачин сохранения полезной ссылки: «запомни ссылку …», «сохрани ссылку …».
LINK_PREFIX = re.compile(
    r"^\s*(запомни|сохрани|добавь)\s+ссылку\b[:\-\s]*", re.IGNORECASE
)

FILES_HINT = (
    f"Спроси своими словами — например «{kbconfig.CFG.example('file')}», "
    f"«последний прайс» или «{kbconfig.CFG.example('link')}». Найду и отправлю."
)

HOWTO_ADD = (
    "<b>Как пополнить «Полезное»</b>\n\n"
    "🔗 <b>Ссылка</b> — напиши: «запомни ссылку https://… — зум для планёрок». "
    "Сохраню сразу, подпишу твоим именем.\n\n"
    "📁 <b>Файл</b> — пришли его мне сюда (презентацию, инструкцию, прайс) и одной "
    "фразой напиши, что это и кому пригодится. Сразу положу в библиотеку; в карточке "
    "останется, кто и когда добавил.\n\n"
    "Видео не присылай файлом — оно на Google Диске или в Zoom, значит это ссылка."
)


def showcase(files: FileLibrary, links: LinkBook) -> str:
    """Витрина полезного: файлы названиями и ссылки. Кнопка показывает, что есть; забирают словами."""
    parts = []
    files_part = files.showcase()
    if files_part:
        parts.append(files_part)
    if links.links:
        parts.append(links.summary())
    if not parts:
        return (
            "Пока пусто: ни файлов, ни сохранённых ссылок.\n\n"
            "Знаешь, что стоит сюда добавить? Меню → Полезное → «➕ Как добавить»."
        )
    parts.append(f"<i>{FILES_HINT}</i>")
    return "\n\n".join(parts)
