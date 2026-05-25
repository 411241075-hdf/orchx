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
    # Anthropic
    r".*claude-haiku-4.*": ModelPrice(1.0, 5.0),
    r".*claude-sonnet-4.*": ModelPrice(3.0, 15.0),
    # Fast-варианты Opus тарифицируются по повышенной ставке — проверяем раньше generic паттерна.
    r".*claude-opus-4.*-fast.*": ModelPrice(30.0, 150.0),
    r".*claude-opus-4.*": ModelPrice(5.0, 25.0),
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
