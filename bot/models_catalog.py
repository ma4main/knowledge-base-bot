"""Список моделей, между которыми можно переключаться прямо из Telegram.

Отобраны по трём признакам, важным именно для нашей задачи:
поддержка кэширования префикса, надёжный вызов инструментов, приемлемый русский.
Цены — справочные, актуальные подтягиваются из OpenRouter при показе.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

MODELS_URL = "https://openrouter.ai/api/v1/models"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    note: str


# Порядок — от дорогих к дешёвым. Цены в скобках — справочная прикидка одного
# вопроса; актуальные подтягиваются из OpenRouter при показе. Пометка «Китай» —
# не оценка качества, а указание, куда уходят данные.
CATALOG: list[ModelInfo] = [
    ModelInfo(
        "anthropic/claude-sonnet-5",
        "Claude Sonnet 5",
        "Эталон (~$0.030). Дороже всех — держим как образец для сравнения.",
    ),
    ModelInfo(
        "google/gemini-3.6-flash",
        "Gemini 3.6 Flash",
        "~$0.023. Сильный русский, дешевле эталона на четверть.",
    ),
    ModelInfo(
        "openai/gpt-5.6-luna",
        "GPT-5.6 Luna",
        "~$0.016. Вдвое дешевле, аккуратно следует инструкциям.",
    ),
    ModelInfo(
        "z-ai/glm-5.2",
        "GLM 5.2",
        "~$0.012. Хорошее соотношение цены и качества. Китай.",
    ),
    ModelInfo(
        "google/gemini-3.7-flash",
        "Gemini 3.7 Flash",
        "~$0.006. Преемник 3 Flash (preview): вход −25%, выход −38%, кэш дешевле.",
    ),
    ModelInfo(
        "deepseek/deepseek-v4-pro",
        "DeepSeek V4 Pro",
        "~$0.006. Очень дешёвый кэш. Китай.",
    ),
    ModelInfo(
        "google/gemini-3.5-flash-lite",
        "Gemini 3.5 Flash-Lite",
        "~$0.005. Заявлена под простые задачи в помощь основной модели.",
    ),
    ModelInfo(
        "minimax/minimax-m3",
        "MiniMax M3",
        "~$0.0045. Вшестеро дешевле эталона. Китай.",
    ),
    ModelInfo(
        "tencent/hy3",
        "Tencent Hy3",
        "~$0.002. В пятнадцать раз дешевле эталона. Китай.",
    ),
    ModelInfo(
        "deepseek/deepseek-v4-flash",
        "DeepSeek V4 Flash",
        "~$0.0014. Самый дешёвый из вменяемых, в 20 раз дешевле эталона. Китай.",
    ),
]

BY_ID = {m.id: m for m in CATALOG}

# Эталон, с которым сравниваем цену: показываем только то, что не дороже него.
REFERENCE_MODEL = "anthropic/claude-sonnet-5"

# Живой список моделей OpenRouter. Заполняется при первом обращении.
_live: dict[str, dict] = {}


async def fetch_live_catalog() -> dict[str, dict]:
    """Модели OpenRouter, пригодные для бота: умеют инструменты, кэшируют префикс, не дороже эталона по входу."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(MODELS_URL)
            response.raise_for_status()
            data = response.json().get("data", [])
    except Exception:
        log.warning("Не удалось получить список моделей OpenRouter", exc_info=True)
        return {}

    def price(item: dict, key: str) -> float:
        try:
            return float((item.get("pricing") or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    reference = next((i for i in data if i.get("id") == REFERENCE_MODEL), None)
    ceiling = price(reference, "prompt") if reference else 0.000002

    result: dict[str, dict] = {}
    for item in data:
        model_id = item.get("id", "")
        params = item.get("supported_parameters") or []
        prompt_price = price(item, "prompt")
        if "tools" not in params:
            continue
        if not price(item, "input_cache_read"):
            continue
        if prompt_price > ceiling or prompt_price <= 0:
            continue
        result[model_id] = {
            "id": model_id,
            "name": item.get("name", model_id),
            "in": prompt_price * 1_000_000,
            "out": price(item, "completion") * 1_000_000,
            "cache_read": price(item, "input_cache_read") * 1_000_000,
            "context": item.get("context_length", 0),
        }

    _live.clear()
    _live.update(result)
    log.info("Живой список OpenRouter: %d моделей не дороже эталона", len(result))
    return result


def label_for(model_id: str) -> str:
    """Человекочитаемое имя: у отобранных своё, у остальных — сам id."""
    known = BY_ID.get(model_id)
    return known.label if known else model_id


def is_known(model_id: str) -> bool:
    """Переключаться можно на отобранную модель или на любую из живого списка."""
    return model_id in BY_ID or model_id in _live


def estimate_question_cost(price: dict) -> float:
    """Прикидка цены одного вопроса: ~5 тыс. токенов кэшируемого префикса, ~12 тыс. единиц базы, ~500 ответа."""
    return (
        12_000 * price["in"] / 1_000_000
        + 5_000 * price["cache_read"] / 1_000_000
        + 500 * price["out"] / 1_000_000
    )


def format_live(current: str, models: dict[str, dict], limit: int = 20) -> str:
    if not models:
        return "Не смог получить список моделей из OpenRouter. Попробуй позже."
    rows = sorted(models.values(), key=lambda m: estimate_question_cost(m))
    lines = [
        f"<b>Все модели не дороже эталона</b> ({len(models)} шт.)",
        "Отобраны те, что умеют инструменты и кэширование.",
        "",
    ]
    for item in rows[:limit]:
        mark = "✅" if item["id"] == current else "▫️"
        lines.append(
            f"{mark} <code>{item['id']}</code>\n"
            f"    ~${estimate_question_cost(item):.4f} за вопрос "
            f"(${item['in']:.2f}/${item['out']:.2f} за млн)"
        )
    if len(rows) > limit:
        lines.append(f"\n…и ещё {len(rows) - limit}. Показаны самые выгодные.")
    return "\n".join(lines)


async def fetch_prices() -> dict[str, dict[str, float]]:
    """Актуальные цены из OpenRouter, $ за миллион токенов. При недоступности — пустой словарь."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(MODELS_URL)
            response.raise_for_status()
            data = response.json().get("data", [])
    except Exception:
        log.warning("Не удалось получить цены из OpenRouter", exc_info=True)
        return {}

    prices: dict[str, dict[str, float]] = {}
    for item in data:
        model_id = item.get("id")
        if model_id not in BY_ID:
            continue
        pricing = item.get("pricing") or {}

        def per_million(key: str) -> float:
            try:
                return float(pricing.get(key) or 0) * 1_000_000
            except (TypeError, ValueError):
                return 0.0

        prices[model_id] = {
            "in": per_million("prompt"),
            "out": per_million("completion"),
            "cache_read": per_million("input_cache_read"),
        }
    return prices


def format_catalog(current: str, prices: dict[str, dict[str, float]]) -> str:
    lines = ["<b>Модель, на которой работает бот</b>", ""]
    for model in CATALOG:
        mark = "✅" if model.id == current else "▫️"
        price = prices.get(model.id)
        if price and price["in"]:
            tail = f"${price['in']:.2f} / ${price['out']:.2f} за млн, из кэша ${price['cache_read']:.2f}"
        else:
            tail = "цена недоступна"
        lines.append(f"{mark} <b>{model.label}</b>\n    {tail}\n    <i>{model.note}</i>")
    lines.append("")
    lines.append("Переключить — кнопкой ниже. Перезапуск не нужен, действует сразу.")
    return "\n".join(lines)
