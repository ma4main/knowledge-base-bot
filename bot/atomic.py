"""Атомарная запись файла: сначала во временный, потом подмена.

Обычный `write_text` сначала усекает файл до нуля; обрыв в этот момент оставляет
пустой `state.json`, и загрузчик берёт значения по умолчанию. Временный файл создаётся
в той же папке (`os.replace` атомарен только внутри одной файловой системы),
сбрасывается на диск и подменяется одним системным вызовом.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Записывает текст так, что файл на диске всегда остаётся целым."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding=encoding, dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    try:
        with handle as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, path)
    except BaseException:
        # Временный файл не должен копиться в папке данных, если подмена не удалась.
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
