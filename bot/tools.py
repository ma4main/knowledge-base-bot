"""Инструменты, доступные модели при ответе на вопрос: только ЧТЕНИЕ базы и файлов.

Записи в базу здесь нет намеренно: по принципу «человек в цикле» правки
живут отдельно — в kb_editor.py, и только с подтверждением руководителя.
"""

from __future__ import annotations

import logging
from typing import Any

from files_lib import FileEntry, FileLibrary
from kb import KnowledgeBase

log = logging.getLogger(__name__)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_kb_units",
            "description": (
                "Загрузить единицы базы знаний целиком по их id. "
                "Основной инструмент: выбери подходящие kb-id по каталогу (INDEX) "
                "и загрузи их одним вызовом. Сколько брать — по вопросу: на конкретный "
                "вопрос про величину, срок или правило обычно хватает одной-двух "
                "единиц, на разбор ситуации бывает нужно три-четыре. Лишние единицы "
                "не делают ответ полнее, только длиннее."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Список id, например [\"kb-101\", \"kb-104\"].",
                    }
                },
                "required": ["ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": (
                "Запасной поиск по словам во всём тексте базы. "
                "Используй, только если по каталогу не удалось понять, где искать, "
                "или если загруженные единицы не содержат ответа. "
                "Возвращает заголовки найденных единиц — сам текст потом бери через get_kb_units."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Ключевые слова на русском, например «срок договора расторжение».",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "Найти файл в библиотеке (презентации, документы, инструкции, прайсы). "
                "Используй, когда менеджер просит прислать файл, презентацию, документ "
                "или «последнюю версию чего-то». Возвращает описания с file-id, датой и тегами. "
                "Файлы не выдумывай — если поиск пуст, так и скажи."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "О чём файл, например «презентация для клиентов» или «прайс-лист».",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_file",
            "description": (
                "Отправить менеджеру файл по его file-id (id берётся из find_files). "
                "После вызова файл уходит менеджеру — в тексте ответа просто подтверди отправку. "
                "Отправляй только то, что менеджер действительно просил."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "id файла из find_files, например file-geo-prezentaciya.",
                    }
                },
                "required": ["file_id"],
            },
        },
    },
]


class ToolRunner:
    def __init__(self, knowledge: KnowledgeBase, files: FileLibrary) -> None:
        self.kb = knowledge
        self.files = files

    def run(
        self,
        name: str,
        arguments: dict[str, Any],
        files_sink: list[FileEntry] | None = None,
        units_sink: list[str] | None = None,
    ) -> str:
        # files_sink копит файлы к отправке — их шлёт обработчик после ответа модели.
        # units_sink копит единицы, на которых построен ответ: по ним работают
        # адресный сброс кэша ответов и кнопка «Подробнее».
        handler = {
            "get_kb_units": lambda args: self._get_units(args, units_sink),
            "search_kb": self._search,
            "find_files": self._find_files,
            "request_file": lambda args: self._request_file(args, files_sink),
        }.get(name)
        if handler is None:
            return f"Неизвестный инструмент: {name}"
        try:
            return handler(arguments)
        except Exception:
            log.exception("Ошибка в инструменте %s с аргументами %r", name, arguments)
            return f"Инструмент {name} завершился ошибкой. Попробуй другой запрос."

    def _get_units(
        self, arguments: dict[str, Any], sink: list[str] | None = None
    ) -> str:
        ids = arguments.get("ids") or []
        if isinstance(ids, str):
            ids = [ids]
        found, missing = self.kb.get_many(ids)
        log.info("get_kb_units: запрошено %s, найдено %d", ids, len(found))
        if sink is not None:
            sink.extend(u.id for u in found if u.id not in sink)

        parts = []
        if missing:
            parts.append(
                "Не найдены (таких id в базе нет, не выдумывай их содержимое): "
                + ", ".join(missing)
            )
        for unit in found:
            # Статус пишем прямо в шапке куска, а не оставляем модели догадываться
            # по frontmatter.
            mark = ""
            if unit.status == "outdated":
                mark = "  ⛔ УСТАРЕЛО — как действующий факт не использовать"
            elif unit.status == "needs-check":
                mark = "  ⚠️ НЕ ПОДТВЕРЖДЕНО — упоминая, скажи, что это на проверке"
            parts.append(f"===== {unit.id} · {unit.title}{mark} =====\n{unit.text}")
        return "\n\n".join(parts) if parts else "Ничего не найдено."

    def _search(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        hits = self.kb.search(query)
        log.info("search_kb %r: %d совпадений", query, len(hits))
        if not hits:
            return f"По запросу «{query}» в базе ничего не найдено."
        lines = [f"- {u.id} · {u.title} (раздел {u.section}, статус {u.status})" for u in hits]
        return "Подходящие единицы:\n" + "\n".join(lines)

    def _find_files(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        hits = self.files.search(query)
        log.info("find_files %r: %d совпадений", query, len(hits))
        fallback = ""
        if not hits:
            # Совпадений нет — показываем всю библиотеку: пустой ответ инструмента
            # модель превращала в «библиотека пуста».
            hits = sorted(self.files.entries.values(), key=lambda e: e.title.lower())
            if not hits:
                return "Библиотека файлов пуста — в ней нет ни одного файла."
            fallback = (
                f"Точного совпадения по запросу «{query}» нет. "
                f"Вот вся библиотека ({len(hits)}) — выбери подходящее или скажи, что ничего нет:\n"
            )
        lines = []
        for e in hits:
            status = "" if e.available else "  ⚠ файл ещё не загружен, отправить нельзя"
            date = f", выдан {e.given}" if e.given else ""
            tags = f" [{', '.join(e.tags)}]" if e.tags else ""
            lines.append(f"- {e.id} · {e.title}{date}{tags}{status}\n  {e.description[:200]}")
        return (fallback or "Файлы в библиотеке:\n") + "\n".join(lines)

    def _request_file(self, arguments: dict[str, Any], sink: list[FileEntry] | None) -> str:
        file_id = str(arguments.get("file_id") or "").strip()
        entry = self.files.get(file_id)
        if entry is None:
            return f"Файла {file_id} в библиотеке нет. Не выдумывай — предложи поискать иначе."
        if not entry.available:
            return (
                f"Файл «{entry.title}» описан, но ещё не загружен в библиотеку. "
                f"Скажи менеджеру, что файл готовят, и предложи спросить руководителя."
            )
        if sink is not None and all(e.id != entry.id for e in sink):
            sink.append(entry)
        log.info("request_file: %s (%s) поставлен на отправку", entry.id, entry.filename)
        return f"Файл «{entry.title}» ({entry.filename}) отправляется менеджеру."
