"""Один и тот же факт из разных чатов — это один пункт сводки, а не три.

Дубль не выбрасывается: повторяемость — сигнал важности, поэтому повтор
прикрепляется к первому пункту (счётчик + откуда). Сравнение по словам, а не
моделью: пунктов за сутки десятки, а совпадение коротких фраз ловится и так;
модель остаётся на границе (`LIKELY`).
"""

from __future__ import annotations

import re

# Слова, которые есть почти в каждом факте и потому ничего не различают.
# Без них «клики» и «клиент» сравнивались бы через общее «что», «для», «это».
STOP = {
    "и", "в", "во", "не", "на", "с", "со", "а", "но", "что", "это", "как", "то",
    "для", "по", "из", "за", "от", "до", "у", "о", "об", "же", "ли", "бы", "к",
    "при", "или", "уже", "ещё", "еще", "теперь", "надо", "нужно", "будет", "быть",
    "есть", "был", "была", "было", "были", "если", "чтобы", "так", "там", "тут",
    "все", "всё", "всех", "мы", "он", "она", "они", "их", "его", "её", "ее",
}

# Порог, выше которого считаем текст тем же фактом. Ошибка в обе стороны видна
# человеку: дубль прикрепляется, а не удаляется.
SAME = 0.62

# Ниже этого — точно разные факты, модель звать незачем.
LIKELY = 0.45

_WORD = re.compile(r"[а-яёa-z0-9]+")


def tokens(text: str) -> set[str]:
    """Значимые слова факта; цифры и kb-id сохраняются — ими отличаются «до 4 кликов» и «до 6 кликов»."""
    words = _WORD.findall((text or "").lower().replace("ё", "е"))
    return {w for w in words if w not in STOP and len(w) > 2 or w.isdigit()}


def similarity(first: str, second: str) -> float:
    """Доля общих слов от меньшего множества (не Жаккар: короткий и подробный пересказ одного факта — один факт)."""
    left, right = tokens(first), tokens(second)
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return shared / min(len(left), len(right))


def find_duplicate(
    text: str, candidates: dict[str, str], threshold: float = SAME
) -> tuple[str, float] | None:
    """Ищет среди candidates ({ключ: текст}) тот же факт; возвращает (ключ, мера) или None."""
    own = tokens(text)
    # Коротким фактам порог выше: на трёх словах случайное совпадение двух даёт 0.66.
    if len(own) < 4:
        threshold = max(threshold, 0.8)
    best: tuple[str, float] | None = None
    for key, other in candidates.items():
        score = similarity(text, other)
        if score >= threshold and (best is None or score > best[1]):
            best = (key, score)
    return best
