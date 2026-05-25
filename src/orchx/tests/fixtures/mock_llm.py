"""FakeLLMClient — программируемый mock :class:`orchx.agent.llm.LLMClient`.

Используется в integration-тестах (P0.5) чтобы не звать реальный Proxy.
Совместим по API (``chat()`` + ``for_role()`` + properties).

Usage:

.. code-block:: python

   from orchx.tests.fixtures.mock_llm import FakeLLMClient, scripted

   llm = FakeLLMClient(scripted([
       {"text": "PLANNING", "tool_calls": [{"name": "write", "args": {"path": "x.py", "content": "..."}}]},
       {"text": "DONE", "tool_calls": []},
   ]))
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScriptedResponse:
    """Один программируемый ответ LLM в сценарии."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Список tool-call'ов в упрощённой форме: ``[{"name": ..., "args": {...}}, ...]``."""
    finish_reason: str = "stop"
    input_tokens: int = 100
    output_tokens: int = 50


def scripted(responses: list[dict[str, Any]]) -> list[ScriptedResponse]:
    """Удобная фабрика: dict-list → ScriptedResponse-list."""
    out: list[ScriptedResponse] = []
    for r in responses:
        out.append(
            ScriptedResponse(
                text=r.get("text", ""),
                tool_calls=r.get("tool_calls", []),
                finish_reason=r.get("finish_reason", "stop"),
                input_tokens=r.get("input_tokens", 100),
                output_tokens=r.get("output_tokens", 50),
            )
        )
    return out


class _FakeToolCall:
    """Compat-shim для orchx.agent.llm.ToolCall (lazy import to avoid cycles)."""

    def __init__(self, name: str, args: dict[str, Any]):
        self.id = f"call_{uuid.uuid4().hex[:8]}"
        self.name = name
        self.arguments = args
        self.raw = {
            "id": self.id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }


class _FakeChatResponse:
    """Compat-shim для orchx.agent.llm.ChatResponse."""

    def __init__(self, resp: ScriptedResponse):
        self.text = resp.text
        self.tool_calls = [
            _FakeToolCall(c["name"], c.get("args", c.get("arguments", {})))
            for c in resp.tool_calls
        ]
        self.tool_calls_raw = [tc.raw for tc in self.tool_calls]
        self.finish_reason = resp.finish_reason
        self.input_tokens = resp.input_tokens
        self.output_tokens = resp.output_tokens


class FakeLLMClient:
    """Программируемый LLM-клиент для тестов.

    Параметры:
        responses: список :class:`ScriptedResponse`. Возвращаются по очереди
            при каждом ``chat()`` вызове. После исчерпания — повторяется
            последний (или бросается :class:`StopIteration` если ``loop=False``).
        loop: повторять ли последний ответ.
        model: имя модели (для совместимости с :attr:`LLMClient.model`).
    """

    def __init__(
        self,
        responses: list[ScriptedResponse] | None = None,
        *,
        loop: bool = True,
        model: str = "fake-model",
        base_url: str = "https://fake.local",
        effort: str | None = "high",
    ):
        self._responses = responses or []
        self._loop = loop
        self._index = 0
        self._model = model
        self._base_url = base_url
        self._effort = effort
        self.calls: list[dict[str, Any]] = []
        """Записи всех вызовов chat() — для assertions в тестах."""

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def effort(self) -> str | None:
        return self._effort

    def for_role(
        self,
        role: str,  # noqa: ARG002
        *,
        effort: str | None = None,
    ) -> FakeLLMClient:
        # Совместная очередь responses — child делит её с parent (чтобы тесты
        # видели единую последовательность).
        child = FakeLLMClient(
            self._responses,
            loop=self._loop,
            model=self._model,
            base_url=self._base_url,
            effort=effort or self._effort,
        )
        child._index = self._index  # noqa: SLF001
        child.calls = self.calls
        return child

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text_delta: Callable[[str], None] | Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[str], None]
        | Callable[[str], Awaitable[None]]
        | None = None,
    ) -> _FakeChatResponse:
        self.calls.append(
            {
                "messages": [m.copy() for m in messages],
                "tools_count": len(tools or []),
                "model": self._model,
                "effort": self._effort,
            }
        )
        if not self._responses:
            return _FakeChatResponse(ScriptedResponse(text="", tool_calls=[]))
        if self._index >= len(self._responses):
            if not self._loop:
                raise RuntimeError(
                    f"FakeLLMClient exhausted after {self._index} calls"
                )
            resp = self._responses[-1]
        else:
            resp = self._responses[self._index]
            self._index += 1
        # on_text_delta — для совместимости (тесты могут проверять, что
        # callback вызывался хотя бы раз).
        if on_text_delta and resp.text:
            try:
                maybe = on_text_delta(resp.text)
                if maybe is not None and hasattr(maybe, "__await__"):
                    await maybe
            except Exception:  # noqa: BLE001
                pass
        _ = on_tool_call_delta
        return _FakeChatResponse(resp)


__all__ = ["FakeLLMClient", "ScriptedResponse", "scripted"]
