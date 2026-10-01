"""Клиент OpenRouter (OpenAI-совместимый /chat/completions).

Для моделей Anthropic кэш префикса не включается сам: нужен явный `cache_control`
на последнем блоке системного промпта, иначе повторяющийся префикс оплачивается
по полной в каждом запросе.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

import alerts

from .base import Completion, ToolCall, Usage

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        fallbacks: list[str] | None = None,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.fallbacks = fallbacks or []
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter просит идентифицировать приложение в статистике.
                "X-Title": "KB Bot",
            },
        )

    async def complete(
        self,
        system_blocks: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> Completion:
        # model задаётся на каждый запрос: её переключают из Telegram без перезапуска.
        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system_blocks}, *messages],
            "usage": {"include": True},  # чтобы видеть, сработал ли кэш
        }
        if tools:
            payload["tools"] = tools
        if self.fallbacks:
            payload["models"] = [model, *self.fallbacks]

        # Сбои и успехи считает модуль критичных сигналов (alerts).
        try:
            response = await self._client.post(API_URL, json=payload)
        except Exception as error:
            alerts.fail(alerts.LLM, f"сеть: {error!r}")
            raise
        if response.status_code >= 400:
            alerts.fail(alerts.LLM, f"вернул {response.status_code}: {response.text[:200]}")
            raise RuntimeError(
                f"OpenRouter вернул {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        alerts.ok(alerts.LLM)

        if not data.get("choices"):
            # OpenRouter умеет отдавать 200 с телом-ошибкой или пустым choices
            # (контент-фильтр, пустой фолбэк) — поднимаем понятную ошибку, а не IndexError.
            raise RuntimeError(f"Неожиданный ответ OpenRouter: {json.dumps(data)[:500]}")

        message = data["choices"][0]["message"]
        usage = _parse_usage(data.get("usage") or {})
        log.info("Модель %s | %s", data.get("model", model), usage)
        # Предупреждаем только если кэшировать было что: в запросе есть блок с cache_control.
        cacheable = any(block.get("cache_control") for block in system_blocks)
        if cacheable and usage.prompt_tokens > 2000 and usage.cached_tokens == 0:
            log.warning(
                "Кэш префикса не сработал (%d входных токенов, из кэша 0). "
                "Проверь, что маршрут %s поддерживает cache_control.",
                usage.prompt_tokens,
                data.get("model", model),
            )

        return Completion(
            text=message.get("content") or "",
            tool_calls=_parse_tool_calls(message.get("tool_calls") or []),
            usage=usage,
            raw_message=message,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    calls = []
    for item in raw:
        function = item.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            log.warning("Не разобрал аргументы инструмента: %r", function.get("arguments"))
            arguments = {}
        calls.append(
            ToolCall(id=item.get("id", ""), name=function.get("name", ""), arguments=arguments)
        )
    return calls


def _parse_usage(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=raw.get("prompt_tokens", 0),
        completion_tokens=raw.get("completion_tokens", 0),
        cached_tokens=details.get("cached_tokens", 0),
        cache_write_tokens=details.get("cache_write_tokens", 0),
        cost=raw.get("cost", 0.0) or 0.0,
        cache_discount=raw.get("cache_discount", 0.0) or 0.0,
    )
