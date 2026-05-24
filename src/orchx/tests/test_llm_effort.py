"""Тесты mapping'а orchX-effort → provider-specific параметры LLM."""

from __future__ import annotations

import pytest

from orchx.agent.llm import _effort_extra_body


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_4_6_uses_adaptive_thinking() -> None:
    body = _effort_extra_body("high", "claude-sonnet-4-6")
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}


def test_anthropic_xhigh_preserves_xhigh_level() -> None:
    """Opus 4.7 поддерживает xhigh — мы не должны мапить его в `max`."""
    body = _effort_extra_body("xhigh", "claude-opus-4-7")
    assert body["output_config"]["effort"] == "xhigh"


def test_anthropic_4_7_adds_summarized_display() -> None:
    """Opus 4.7+ требует display='summarized', иначе thinking приходит пустым."""
    body = _effort_extra_body("high", "claude-opus-4-7")
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_anthropic_through_openrouter_proxy_path_recognized() -> None:
    """Реальный кейс: модель `just-ai/openrouter-proxy/anthropic/claude-...`."""
    body = _effort_extra_body(
        "high", "just-ai/openrouter-proxy/anthropic/claude-sonnet-4-6"
    )
    assert "thinking" in body
    assert body["output_config"]["effort"] == "high"


def test_anthropic_minimal_effort_maps_to_low() -> None:
    body = _effort_extra_body("minimal", "claude-haiku-4-5")
    assert body["output_config"]["effort"] == "low"


# ---------------------------------------------------------------------------
# OpenAI o-series / GPT-5
# ---------------------------------------------------------------------------


def test_openai_o_series_uses_reasoning_effort() -> None:
    body = _effort_extra_body("high", "openai/o3")
    assert body == {"reasoning_effort": "high"}


def test_openai_o4_mini() -> None:
    body = _effort_extra_body("medium", "o4-mini")
    assert body == {"reasoning_effort": "medium"}


def test_gpt_5_uses_reasoning_effort() -> None:
    body = _effort_extra_body("high", "gpt-5")
    assert body == {"reasoning_effort": "high"}


def test_gpt_5_1_uses_reasoning_effort() -> None:
    body = _effort_extra_body("xhigh", "openai/gpt-5.1")
    assert body == {"reasoning_effort": "xhigh"}


def test_gpt_4_does_not_get_reasoning_effort() -> None:
    """GPT-4 не reasoning-модель — не насилуем её параметром."""
    body = _effort_extra_body("high", "gpt-4o")
    assert body == {}


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_uses_thinking_budget() -> None:
    body = _effort_extra_body("high", "gemini-2.5-pro")
    assert "thinking_config" in body
    assert body["thinking_config"]["thinking_budget"] == 12288


def test_gemini_max_uses_dynamic() -> None:
    body = _effort_extra_body("max", "google/gemini-2.5-flash")
    assert body["thinking_config"]["thinking_budget"] == -1


def test_gemini_minimal_disables_thinking() -> None:
    body = _effort_extra_body("minimal", "gemini-2.5-flash-lite")
    assert body["thinking_config"]["thinking_budget"] == 0


# ---------------------------------------------------------------------------
# DeepSeek + others
# ---------------------------------------------------------------------------


def test_deepseek_uses_reasoning_effort() -> None:
    body = _effort_extra_body("high", "deepseek-r1")
    assert body == {"reasoning_effort": "high"}


def test_unknown_model_returns_empty() -> None:
    """Unknown model — пустой extra_body, не пытаемся форсить reasoning."""
    body = _effort_extra_body("high", "qwen-2.5-coder")
    assert body == {}


def test_no_effort_returns_empty() -> None:
    body = _effort_extra_body(None, "claude-sonnet-4-6")
    assert body == {}
    body = _effort_extra_body("", "claude-sonnet-4-6")
    assert body == {}


# ---------------------------------------------------------------------------
# Параметризация
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "effort,expected_budget",
    [
        ("minimal", 0),
        ("low", 1024),
        ("medium", 4096),
        ("high", 12288),
        ("xhigh", 24576),
        ("max", -1),
    ],
)
def test_gemini_budget_table(effort: str, expected_budget: int) -> None:
    body = _effort_extra_body(effort, "gemini-2.5-pro")
    assert body["thinking_config"]["thinking_budget"] == expected_budget
