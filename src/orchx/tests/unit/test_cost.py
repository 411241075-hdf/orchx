"""Тесты cost tracking (P1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.cost import ModelPrice, estimate_cost_usd, load_overrides


def test_estimate_cost_known_model():
    cost = estimate_cost_usd(
        model="gpt-4o", input_tokens=1_000_000, output_tokens=1_000_000
    )
    # gpt-4o: 2.5 input + 10.0 output = 12.5 per 1M+1M.
    assert abs(cost - 12.5) < 0.001


def test_estimate_cost_zero_tokens_is_zero():
    assert estimate_cost_usd(model="gpt-4o", input_tokens=0, output_tokens=0) == 0.0


def test_estimate_cost_unknown_model_returns_zero():
    assert estimate_cost_usd(
        model="completely-unknown-model-xyz",
        input_tokens=1000,
        output_tokens=1000,
    ) == 0.0


def test_estimate_cost_fake_model_is_zero():
    """fake-model используется в тестах — обязан быть 0."""
    cost = estimate_cost_usd(
        model="fake-model", input_tokens=1000, output_tokens=1000
    )
    assert cost == 0.0


def test_estimate_cost_claude_sonnet_pattern():
    cost = estimate_cost_usd(
        model="claude-sonnet-4.5-20250607",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost > 0  # paterns матчат


def test_estimate_cost_haiku_pattern():
    cost = estimate_cost_usd(
        model="claude-3-haiku-20240307",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert 0 < cost < 5  # haiku — дешёвый


def test_estimate_cost_with_override():
    overrides = {"my-custom-model": ModelPrice(5.0, 20.0)}
    cost = estimate_cost_usd(
        model="my-custom-model",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        overrides=overrides,
    )
    assert cost == 25.0


def test_load_overrides_from_missing_file_returns_empty(tmp_path: Path):
    assert load_overrides(tmp_path / "nope.yaml") == {}


def test_load_overrides_from_yaml(tmp_path: Path):
    p = tmp_path / "costs.yaml"
    p.write_text(
        '"my-model": {input: 1.5, output: 5.0}\n"other": {input: 0.5, output: 2.0}\n',
        encoding="utf-8",
    )
    out = load_overrides(p)
    assert "my-model" in out
    assert out["my-model"].input_per_million == 1.5
    assert out["my-model"].output_per_million == 5.0
    assert out["other"].output_per_million == 2.0


def test_load_overrides_skips_invalid_entries(tmp_path: Path):
    p = tmp_path / "costs.yaml"
    p.write_text(
        '"valid": {input: 1.0, output: 2.0}\n"invalid": "not a dict"\n',
        encoding="utf-8",
    )
    out = load_overrides(p)
    assert "valid" in out
    assert "invalid" not in out


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "o1-mini",
        "claude-3-5-sonnet-20241022",
        "deepseek-chat",
        "gemini-2.0-flash-exp",
        "llama-3.1-8b-instant",
    ],
)
def test_known_models_have_nonzero_price(model: str):
    cost = estimate_cost_usd(model=model, input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost > 0, f"model {model} has zero price"
