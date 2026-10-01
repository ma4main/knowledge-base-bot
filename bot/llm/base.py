"""Единый интерфейс к модели. Провайдер меняется правкой конфига, а не бота."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    cache_discount: float = 0.0

    def __str__(self) -> str:
        share = (
            f"{100 * self.cached_tokens / self.prompt_tokens:.0f}%"
            if self.prompt_tokens
            else "н/д"
        )
        # cache_discount OpenRouter на части маршрутов не заполняет — показываем
        # только если он реально пришёл, чтобы не пугать нулём.
        discount = (
            f", экономия на кэше=${self.cache_discount:.5f}" if self.cache_discount else ""
        )
        return (
            f"in={self.prompt_tokens} (из кэша {self.cached_tokens}, {share}), "
            f"запись в кэш={self.cache_write_tokens}, out={self.completion_tokens}, "
            f"стоимость=${self.cost:.5f}{discount}"
        )


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw_message: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    async def complete(
        self,
        system_blocks: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> Completion: ...

    async def aclose(self) -> None: ...
