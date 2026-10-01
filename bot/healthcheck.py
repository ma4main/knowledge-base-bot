"""Проверка здоровья контейнера. Запускается самим Docker, см. HEALTHCHECK.

Отдельный файл, а не однострочник в compose: проверку надо уметь запустить руками
и прочитать её ответ, а не расшифровывать код возврата.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import heartbeat


def main() -> int:
    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    if heartbeat.is_fresh(data_dir):
        print("ok: отметка живости свежая")
        return 0
    target = heartbeat.path_for(data_dir)
    if not target.is_file():
        print(f"НЕЗДОРОВ: отметки {target} нет — бот не дошёл до первой проверки Telegram")
    else:
        print(f"НЕЗДОРОВ: отметка старше {heartbeat.STALE_AFTER} с — "
              f"polling завис или Telegram недоступен")
    return 1


# Работа только при прямом запуске: selfcheck импортирует каждый модуль отдельно,
# и падение на импорте выглядело бы как сломанный модуль.
if __name__ == "__main__":
    sys.exit(main())
