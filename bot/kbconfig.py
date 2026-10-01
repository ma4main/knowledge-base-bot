"""Профиль компании и разделы базы: `knowledge/_config.json`.

Загружается при импорте из `KB_ROOT` (или корня репозитория), чтобы промпты,
меню и классификатор могли подставить название компании, продукты и разделы
на уровне модуля. `init()` перечитывает профиль из другого корня.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REL_PATH = "knowledge/_config.json"


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    id_from: int
    id_to: int
    description: str
    product: str = ""


@dataclass(frozen=True)
class KBConfig:
    company: str
    department: str
    audience: str
    products: dict[str, str]
    sections: dict[str, Section]
    examples: dict[str, str] = field(default_factory=dict)

    @property
    def product_names(self) -> list[str]:
        return list(self.products)

    @property
    def section_titles(self) -> dict[str, str]:
        return {key: s.title for key, s in self.sections.items()}

    @property
    def products_text(self) -> str:
        """«Трафик» (описание), «Карты» (описание) — для промптов; пусто, если продуктов нет."""
        return ", ".join(f"«{name}» ({desc})" for name, desc in self.products.items())

    @property
    def who(self) -> str:
        """«аккаунт-менеджеры компании «Маяк»» — для промптов."""
        return f"{self.audience} компании «{self.company}»"

    def example(self, kind: str, default: str = "") -> str:
        return self.examples.get(kind) or default


_DEFAULT = KBConfig(
    company="компания",
    department="отдел",
    audience="сотрудники",
    products={},
    sections={},
    examples={
        "question": "какой сейчас тариф",
        "file": "пришли последнюю презентацию",
        "link": "дай ссылку на зум",
    },
)


def load(root: Path) -> KBConfig:
    path = root / REL_PATH
    if not path.is_file():
        return _DEFAULT
    raw = json.loads(path.read_text(encoding="utf-8"))
    sections: dict[str, Section] = {}
    for key, item in (raw.get("sections") or {}).items():
        ids = item.get("ids") or [0, 0]
        sections[key] = Section(
            key=key,
            title=str(item.get("title") or key),
            id_from=int(ids[0]),
            id_to=int(ids[1]),
            description=str(item.get("description") or ""),
            product=str(item.get("product") or ""),
        )
    examples = dict(_DEFAULT.examples)
    examples.update({str(k): str(v) for k, v in (raw.get("examples") or {}).items()})
    return KBConfig(
        company=str(raw.get("company") or _DEFAULT.company),
        department=str(raw.get("department") or _DEFAULT.department),
        audience=str(raw.get("audience") or _DEFAULT.audience),
        products={str(k): str(v) for k, v in (raw.get("products") or {}).items()},
        sections=sections,
        examples=examples,
    )


def _default_root() -> Path:
    return Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent)


CFG: KBConfig = load(_default_root())


def init(root: Path) -> KBConfig:
    """Перечитать профиль из указанного корня и сделать его текущим."""
    global CFG
    CFG = load(root)
    return CFG


def current() -> KBConfig:
    return CFG
