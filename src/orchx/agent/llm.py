"""OpenAI-совместимый клиент для общения с LLM Proxy.

Тонкая обёртка над ``openai.AsyncOpenAI``: стрим, агрегация tool_calls-дельт
по ``index``, маппинг reasoning effort в provider-specific поля
(``reasoning_effort`` для OpenAI o-series; ``thinking`` для Anthropic-моделей).

Зависимостей вне ``openai``-SDK у клиента нет — никаких httpx-обвязок поверх.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    """Параметры подключения к Proxy."""

    base_url: str
    api_key: str
    model: str
    effort: str | None = None
    """Маппится в provider-specific reasoning effort (``low|medium|high|xhigh``)."""
    timeout_s: float = 600.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    """Произвольные заголовки (например, для трассировки)."""

    @classmethod
    def from_env(cls, *, model_override: str | None = None) -> LLMConfig:
        """Загрузить конфиг из переменных окружения.

        Обязательные:
            ``ORCHX_LLM_BASE_URL``, ``ORCHX_LLM_API_KEY``, ``ORCHX_MODEL``.

        Опциональные:
            ``ORCHX_TIMEOUT_S`` (default 600).

        Args:
            model_override: Если задан, заменяет ``ORCHX_MODEL`` (используется
                для per-role overrides).

        Raises:
            RuntimeError: Если хотя бы одна обязательная переменная не задана.
        """
        base_url = os.environ.get("ORCHX_LLM_BASE_URL", "").strip()
        api_key = os.environ.get("ORCHX_LLM_API_KEY", "").strip()
        model = (model_override or os.environ.get("ORCHX_MODEL", "")).strip()
        missing: list[str] = []
        if not base_url:
            missing.append("ORCHX_LLM_BASE_URL")
        if not api_key:
            missing.append("ORCHX_LLM_API_KEY")
        if not model:
            missing.append("ORCHX_MODEL")
        if missing:
            raise RuntimeError(
                "orchX: missing required env vars: "
                + ", ".join(missing)
                + ". Set them to your OpenAI-compatible Proxy endpoint, key, and "
                "model id (e.g. anthropic/claude-opus-4-7)."
            )
        try:
            timeout = float(os.environ.get("ORCHX_TIMEOUT_S", "600"))
        except ValueError:
            timeout = 600.0
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_s=timeout,
        )


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """Распарсенный tool-call из ответа LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    """Уже распарсенный JSON. Если LLM прислал невалидный JSON — пустой dict."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Сырая запись для добавления в ``messages`` (формат OpenAI tool_call)."""


@dataclass
class ChatResponse:
    """Финальная склейка одного хода LLM."""

    text: str
    """Текстовый контент ассистента (может быть пустой строкой)."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_calls_raw: list[dict[str, Any]] = field(default_factory=list)
    """Список tool_call'ов в формате, который кладётся в messages[role=assistant]."""

    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# Effort mapping
# ---------------------------------------------------------------------------


_THINKING_BUDGET = {
    "minimal": 1024,
    "low": 1024,
    "medium": 4000,
    "high": 16000,
    "xhigh": 32000,
    "max": 32000,
}


def _effort_extra_body(effort: str | None, model: str) -> dict[str, Any]:
    """Сконвертировать orchX-effort в provider-specific extra_body.

    OpenAI o-series → ``reasoning_effort``.
    Anthropic (Claude через OpenRouter / Proxy) → ``thinking.budget_tokens``.
    Прочее → пробуем ``reasoning_effort`` (если Proxy не знает — проигнорирует).
    """
    if not effort:
        return {}
    model_l = model.lower()
    is_anthropic = "claude" in model_l or "anthropic" in model_l
    is_openai_o = (
        model_l.startswith(("openai/o", "o"))
        and not is_anthropic
        and "openai/gpt" not in model_l
    )
    if is_openai_o:
        return {"reasoning_effort": effort}
    if is_anthropic:
        budget = _THINKING_BUDGET.get(effort, 16000)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {"reasoning_effort": effort}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Тонкий стрим-клиент к OpenAI-совместимому Proxy.

    Создаётся один раз на запуск роя; ``for_role()`` отдаёт «дочерний» клиент
    с правильной моделью и effort'ом для конкретной роли (planner/reviewer/...).
    """

    def __init__(self, cfg: LLMConfig):
        """Создать клиент с заданным :class:`LLMConfig`."""
        # ленивый импорт openai-SDK: не делаем его hard-dep самого пакета,
        # тесты подкладывают мок.
        from openai import AsyncOpenAI

        self._cfg = cfg
        self._client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout_s,
            default_headers=cfg.extra_headers or None,
        )

    @property
    def model(self) -> str:
        """Текущая модель клиента."""
        return self._cfg.model

    @property
    def base_url(self) -> str:
        """Endpoint Proxy."""
        return self._cfg.base_url

    @property
    def effort(self) -> str | None:
        """Текущий effort клиента."""
        return self._cfg.effort

    def for_role(
        self,
        role: str,
        *,
        effort: str | None = None,
    ) -> LLMClient:
        """Создать дочерний клиент с per-role override модели/effort'а.

        Modeл'и берутся из env: ``ORCHX_<ROLE>_MODEL`` (например,
        ``ORCHX_PLANNER_MODEL``). Если не задана — используется дефолтная
        ``ORCHX_MODEL``.
        """
        env_key = f"ORCHX_{role.upper()}_MODEL"
        model = os.environ.get(env_key, "").strip() or self._cfg.model
        new_cfg = LLMConfig(
            base_url=self._cfg.base_url,
            api_key=self._cfg.api_key,
            model=model,
            effort=effort or self._cfg.effort,
            timeout_s=self._cfg.timeout_s,
            extra_headers=dict(self._cfg.extra_headers),
        )
        return LLMClient(new_cfg)

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text_delta: (
            Callable[[str], None] | Callable[[str], Awaitable[None]] | None
        ) = None,
        on_tool_call_delta: (
            Callable[[str], None] | Callable[[str], Awaitable[None]] | None
        ) = None,
    ) -> ChatResponse:
        """Один стрим-запрос к Proxy с агрегацией ответа.

        Args:
            messages: История диалога в OpenAI-формате.
            tools: Список tool-схем (``{type: "function", function: {...}}``).
                Если пустой/None — отключаем tool-calling для этого хода.
            on_text_delta: Callback на каждую дельту текста ассистента.
                Может быть sync или async; исключения проглатываются.
            on_tool_call_delta: Callback при обнаружении нового tool_call (по
                имени, один раз на каждый id).

        Returns:
            :class:`ChatResponse` с собранным текстом и tool_calls.
        """
        kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        extra = _effort_extra_body(self._cfg.effort, self._cfg.model)
        if extra:
            kwargs["extra_body"] = extra

        # Аккумулирующие буферы. tool_calls приходят дельтами по index.
        text_chunks: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        seen_tool_names: set[int] = set()
        finish_reason: str | None = None

        async with await self._client.chat.completions.create(**kwargs) as stream:  # type: ignore[arg-type]
            async for chunk in stream:
                # finish_reason обычно прилетает в последней дельте.
                try:
                    choice = chunk.choices[0]
                except (AttributeError, IndexError):
                    continue
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                # Текст.
                content_delta = getattr(delta, "content", None)
                if content_delta:
                    text_chunks.append(content_delta)
                    await _maybe_call(on_text_delta, content_delta)
                # Tool calls (могут отсутствовать).
                tc_deltas = getattr(delta, "tool_calls", None) or []
                for tc_delta in tc_deltas:
                    idx = getattr(tc_delta, "index", 0) or 0
                    acc = tool_acc.setdefault(
                        idx,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if getattr(tc_delta, "id", None):
                        acc["id"] = tc_delta.id
                    fn = getattr(tc_delta, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            acc["function"]["name"] += fn.name
                            if idx not in seen_tool_names and acc["function"]["name"]:
                                seen_tool_names.add(idx)
                                await _maybe_call(
                                    on_tool_call_delta, acc["function"]["name"]
                                )
                        if getattr(fn, "arguments", None):
                            acc["function"]["arguments"] += fn.arguments

        # Собираем финальный ответ.
        text = "".join(text_chunks)
        raw_calls = [tool_acc[i] for i in sorted(tool_acc.keys())]
        parsed_calls: list[ToolCall] = []
        for raw in raw_calls:
            args_str = raw.get("function", {}).get("arguments", "") or ""
            try:
                args = json.loads(args_str) if args_str else {}
                if not isinstance(args, dict):
                    args = {}
            except (ValueError, TypeError):
                args = {}
            parsed_calls.append(
                ToolCall(
                    id=raw.get("id") or "",
                    name=raw.get("function", {}).get("name", "") or "",
                    arguments=args,
                    raw=raw,
                )
            )
        return ChatResponse(
            text=text,
            tool_calls=parsed_calls,
            tool_calls_raw=raw_calls,
            finish_reason=finish_reason,
        )


async def _maybe_call(
    cb: Callable[[str], None] | Callable[[str], Awaitable[None]] | None,
    arg: str,
) -> None:
    """Вызвать callback (sync/async/None), не падая на исключениях."""
    if cb is None:
        return
    try:
        result = cb(arg)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]
    except Exception:  # noqa: BLE001
        pass
