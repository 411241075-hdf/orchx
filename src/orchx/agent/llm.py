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

    input_tokens: int = 0
    """Сколько input-токенов взял этот один LLM-вызов (если Proxy сообщает)."""
    output_tokens: int = 0
    """Сколько output-токенов произвёл этот вызов."""


# ---------------------------------------------------------------------------
# Effort mapping
# ---------------------------------------------------------------------------


# Маппинг orchX-effort → Anthropic-effort. orchX использует «xhigh» как
# обобщённый «максимум reasoning»; на Anthropic мы держим расширенный
# набор уровней (``xhigh`` доступен на Opus 4.7 как промежуточное значение
# между ``high`` и ``max``).
_ANTHROPIC_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Gemini thinking budgets — приблизительные эквиваленты.
# Gemini 2.5 принимает int (max ~24576 для Flash, ~32768 для Pro).
# 0 — отключить thinking, -1 — динамический (рекомендация Google).
_GEMINI_THINKING_BUDGET = {
    "minimal": 0,
    "low": 1024,
    "medium": 4096,
    "high": 12288,
    "xhigh": 24576,
    "max": -1,  # dynamic
}


def _is_anthropic(model: str) -> bool:
    """Определить семейство Claude (Anthropic / openrouter passthrough)."""
    m = model.lower()
    if "claude" in m or m.startswith("anthropic/") or "/anthropic/" in m:
        return True
    return False


def _is_anthropic_4_7_or_later(model: str) -> bool:
    """Определить Claude Opus 4.7+ — для них нужно `display: "summarized"`."""
    m = model.lower()
    # `claude-opus-4-7`, `claude-opus-4-8`, …
    return bool(_ANT_47_RE.search(m))


_ANT_47_RE = __import__("re").compile(
    r"claude-(opus|sonnet|haiku)-([4-9]-[7-9]|[5-9]-\d+|\d{2,}-\d+)"
)


def _is_openai_reasoning(model: str) -> bool:
    """OpenAI o-series и GPT-5/5.1 (с reasoning_effort)."""
    m = model.lower()
    # o1, o3, o4-mini, gpt-5, gpt-5.1.
    if "openai/" in m:
        m = m.split("openai/")[-1]
    if m.startswith(("o1", "o3", "o4")):
        return True
    if m.startswith("gpt-5"):
        return True
    return False


def _is_gemini(model: str) -> bool:
    """Google Gemini (через Proxy)."""
    m = model.lower()
    return "gemini" in m


def _is_deepseek(model: str) -> bool:
    """DeepSeek — поддерживает `reasoning_effort` на R-моделях."""
    m = model.lower()
    return "deepseek" in m


def _effort_extra_body(effort: str | None, model: str) -> dict[str, Any]:
    """Сконвертировать orchX-effort в provider-specific extra_body.

    Поддерживаемые семейства моделей:

    - **Anthropic Claude 4.6+** (Sonnet/Opus/Haiku) — adaptive thinking
      (``thinking: {"type": "adaptive"}``) + ``output_config.effort``.
      Для Opus 4.7+ дополнительно выставляется ``display: "summarized"``,
      иначе thinking-блоки приходят пустыми и live-доска не показывает
      прогресс рассуждения.
    - **OpenAI o-series + GPT-5** — top-level ``reasoning_effort``.
    - **Google Gemini 2.5+** — ``thinking_config: {thinking_budget: N}``.
    - **DeepSeek R-серия** — ``reasoning_effort`` (Proxy транслирует).
    - **Прочее** (DeepSeek V, Llama, Qwen non-reasoning, …) — пусто;
      effort просто игнорируется, модель работает в обычном режиме.
    """
    if not effort:
        return {}
    if _is_anthropic(model):
        ant_effort = _ANTHROPIC_EFFORT_MAP.get(effort, "high")
        thinking: dict[str, Any] = {"type": "adaptive"}
        if _is_anthropic_4_7_or_later(model):
            thinking["display"] = "summarized"
        return {
            "thinking": thinking,
            "output_config": {"effort": ant_effort},
        }
    if _is_openai_reasoning(model):
        return {"reasoning_effort": effort}
    if _is_gemini(model):
        budget = _GEMINI_THINKING_BUDGET.get(effort, 4096)
        return {"thinking_config": {"thinking_budget": budget}}
    if _is_deepseek(model):
        return {"reasoning_effort": effort}
    # Любая другая модель — не насилуем её reasoning-параметром.
    return {}


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

        # Запросим, чтобы провайдер вернул usage в стриме (OpenAI standard
        # требует stream_options.include_usage=true). Если Proxy не знает —
        # просто проигнорирует параметр.
        kwargs.setdefault("stream_options", {"include_usage": True})

        # Аккумулирующие буферы. tool_calls приходят дельтами по index.
        text_chunks: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        seen_tool_names: set[int] = set()
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0

        async with await self._client.chat.completions.create(**kwargs) as stream:  # type: ignore[arg-type]
            async for chunk in stream:
                # Usage может прийти в финальном chunk'е (OpenAI/Anthropic).
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = (
                        getattr(usage, "input_tokens", None)
                        or getattr(usage, "prompt_tokens", None)
                        or input_tokens
                    )
                    output_tokens = (
                        getattr(usage, "output_tokens", None)
                        or getattr(usage, "completion_tokens", None)
                        or output_tokens
                    )
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
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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
