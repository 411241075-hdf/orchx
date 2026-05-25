"""Cost tracking + per-model price table (P1.3).

Цены — наилучшая попытка отобразить current rate cards известных провайдеров
(в USD за 1M токенов). Структура — паттерн-based: ключи — regex'ы по
имени модели (которое возвращает provider в response).

Пользователи могут переопределить через ``.orchx/costs.yaml``:

.. code-block:: yaml

   # input/output — USD per 1M tokens
   "gpt-4.1": {input: 2.0, output: 8.0}
   "claude-3-7-sonnet-.*": {input: 3.0, output: 15.0}
   ".*-haiku-.*": {input: 0.25, output: 1.25}

Загрузка происходит лениво. Если файла нет — используется встроенная
таблица (см. ``_DEFAULT_PRICES``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """Цена в USD за 1M токенов."""

    input_per_million: float
    output_per_million: float


# Дефолтные prices — best-effort на момент написания (2025-Q2 публичные rate cards).
# Не претендуем на 100% актуальность; users override через .orchx/costs.yaml.
_DEFAULT_PRICES: dict[str, ModelPrice] = {
    # OpenAI
    r"gpt-4o\b.*": ModelPrice(2.5, 10.0),
    r"gpt-4o-mini\b.*": ModelPrice(0.15, 0.6),
    r"gpt-4\.1$": ModelPrice(2.0, 8.0),
    r"gpt-4\.1-mini": ModelPrice(0.4, 1.6),
    r"gpt-4-turbo.*": ModelPrice(10.0, 30.0),
    r"o1$|o1-preview": ModelPrice(15.0, 60.0),
    r"o1-mini": ModelPrice(3.0, 12.0),
    r"o3$": ModelPrice(20.0, 80.0),
    r"o3-mini": ModelPrice(4.0, 16.0),
    # Anthropic
    r".*claude-3-7-sonnet.*|.*sonnet-4.*|.*claude-sonnet-4.*": ModelPrice(3.0, 15.0),
    r".*claude-3-5-sonnet.*|.*claude-sonnet-3.5.*": ModelPrice(3.0, 15.0),
    r".*claude-3-5-haiku.*|.*haiku-3.5.*": ModelPrice(0.8, 4.0),
    r".*claude-3-haiku.*": ModelPrice(0.25, 1.25),
    r".*claude-3-opus.*|.*claude-opus.*": ModelPrice(15.0, 75.0),
    # Google
    r".*gemini-2\.0-flash.*": ModelPrice(0.075, 0.3),
    r".*gemini-2\.5-pro.*": ModelPrice(1.25, 10.0),
    r".*gemini-1\.5-pro.*": ModelPrice(1.25, 5.0),
    r".*gemini-1\.5-flash.*": ModelPrice(0.075, 0.3),
    # DeepSeek
    r".*deepseek-(chat|v3).*": ModelPrice(0.14, 0.28),
    r".*deepseek-(coder|r1).*": ModelPrice(0.14, 2.19),
    # Open-source / hosted
    r".*qwen.*-coder.*": ModelPrice(0.5, 1.5),
    r".*llama-3\.[12]-70b.*": ModelPrice(0.65, 0.65),
    r".*llama-3\.1-8b.*": ModelPrice(0.05, 0.05),
    # Catch-all fake-model для тестов (zero cost)
    r"^fake-model$": ModelPrice(0.0, 0.0),
}


def _find_price(model: str, overrides: dict[str, ModelPrice]) -> ModelPrice | None:
    """Найти цену для модели по first-match regex'у.

    Сначала проверяем overrides, потом дефолты. Возвращает None если ни одна
    pattern не сматчилась (orchx тогда логирует warning один раз).
    """
    if not model:
        return None
    for pat, price in overrides.items():
        try:
            if re.fullmatch(pat, model):
                return price
        except re.error:
            continue
    for pat, price in _DEFAULT_PRICES.items():
        if re.fullmatch(pat, model):
            return price
    return None


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    overrides: dict[str, ModelPrice] | None = None,
) -> float:
    """Оценить стоимость одного LLM-вызова в USD.

    Returns:
        Float в USD; 0.0 если цена неизвестна или токенов нет.
    """
    price = _find_price(model, overrides or {})
    if price is None:
        _warn_unknown_model(model)
        return 0.0
    return (
        input_tokens / 1_000_000 * price.input_per_million
        + output_tokens / 1_000_000 * price.output_per_million
    )


_WARNED_MODELS: set[str] = set()


def _warn_unknown_model(model: str) -> None:
    if model in _WARNED_MODELS:
        return
    _WARNED_MODELS.add(model)
    logger.info(
        "cost: no price table entry for model %r; cost will be reported as 0.00",
        model,
    )


def load_overrides(path: Path) -> dict[str, ModelPrice]:
    """Прочитать ``.orchx/costs.yaml`` с override'ами.

    Формат:

    .. code-block:: yaml

       "gpt-4.1": {input: 2.0, output: 8.0}
       "regex-pattern": {input: 1.5, output: 6.0}

    Returns:
        dict ``regex_pattern → ModelPrice``. Пустой dict если файл отсутствует
        или некорректен.
    """
    if not path.exists():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, Exception) as e:  # noqa: BLE001
        logger.warning("cost overrides %s could not be loaded: %s", path, e)
        return {}
    out: dict[str, ModelPrice] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            try:
                out[str(k)] = ModelPrice(
                    input_per_million=float(v.get("input", 0)),
                    output_per_million=float(v.get("output", 0)),
                )
            except (TypeError, ValueError):
                continue
    return out


__all__ = [
    "ModelPrice",
    "estimate_cost_usd",
    "load_overrides",
]
