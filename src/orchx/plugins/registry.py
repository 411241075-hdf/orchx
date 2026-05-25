"""Plugin registry: discovery через entry-points + factory + config loader.

См. :mod:`orchx.plugins` для верхнеуровневого описания.

Поведение discovery:

1. Для каждого slot'а (``orchx.runtime``, ``orchx.tracker``, …) собираются
   все entry-points из всех установленных пакетов.
2. ``load_plugin(slot, name)`` находит entry-point по имени и инстанциирует
   его с переданным конфигом (``**config``).
3. ``load_from_config(path)`` читает ``.orchx/config.yaml`` и возвращает
   готовый dict плагинов с уже применёнными per-plugin конфигами.

Сторонние пакеты регистрируют плагины так же — никакого магии:

.. code-block:: toml

   [project.entry-points."orchx.notifier"]
   teams = "my_pkg.teams_notifier:TeamsNotifier"
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SLOTS = ("runtime", "tracker", "scm", "notifier", "memory")


class PluginError(Exception):
    """Базовая ошибка plugin-системы."""


class PluginNotFound(PluginError):
    """Запрошенный plugin не зарегистрирован в указанном slot'е."""


class PluginLoadError(PluginError):
    """Plugin зарегистрирован, но его модуль не удалось загрузить."""


def _eps_for_slot(slot: str) -> list[EntryPoint]:
    """Все entry-points для указанного slot'а (``orchx.runtime`` и т.п.)."""
    if slot not in SLOTS:
        raise ValueError(f"Unknown plugin slot: {slot!r}. Valid: {SLOTS}")
    try:
        return list(entry_points(group=f"orchx.{slot}"))
    except Exception:  # noqa: BLE001
        # Старая API entry_points (<3.10): возвращает dict-like SelectableGroups.
        eps = entry_points()
        # SelectableGroups поддерживает ``.select(group=...)``; dict — ``.get(...)``.
        select = getattr(eps, "select", None)
        if callable(select):
            return list(select(group=f"orchx.{slot}"))
        get = getattr(eps, "get", None)
        if callable(get):
            return list(get(f"orchx.{slot}", []))
        return []


def registered_plugins() -> dict[str, list[str]]:
    """Список всех зарегистрированных plugin'ов по slot'ам.

    Полезно для CLI ``orchx plugins list``.
    """
    return {slot: sorted(ep.name for ep in _eps_for_slot(slot)) for slot in SLOTS}


def load_plugin(slot: str, name: str, *, config: dict[str, Any] | None = None) -> Any:
    """Найти и инстанциировать plugin по имени.

    Args:
        slot: один из ``runtime`` / ``tracker`` / ``scm`` / ``notifier`` / ``memory``.
        name: имя plugin'а (как в entry-point).
        config: dict, передаётся как ``**kwargs`` в конструктор plugin'а.

    Raises:
        PluginNotFound: если plugin не зарегистрирован.
        PluginLoadError: если import фабрики упал.
    """
    cfg = _expand_env_vars(config or {})
    eps = _eps_for_slot(slot)
    for ep in eps:
        if ep.name == name:
            try:
                factory = ep.load()
            except Exception as e:  # noqa: BLE001
                raise PluginLoadError(
                    f"Failed to load plugin {slot}/{name!r} from {ep.value!r}: {e}"
                ) from e
            try:
                return factory(**cfg)
            except TypeError as e:
                raise PluginLoadError(
                    f"Plugin {slot}/{name!r} constructor rejected config "
                    f"{list(cfg)}: {e}"
                ) from e
    available = sorted(ep.name for ep in eps)
    raise PluginNotFound(
        f"Plugin {name!r} not found in slot {slot!r}. Available: {available}"
    )


def load_from_config(config_path: Path | str) -> dict[str, Any]:
    """Прочитать ``.orchx/config.yaml`` и загрузить все объявленные plugin'ы.

    Возвращает dict:

    .. code-block:: python

       {
         "runtime": <RuntimePlugin>,
         "tracker": <TrackerPlugin>,
         "scm": <SCMPlugin>,
         "notifiers": [<NotifierPlugin>, ...],  # NB: множественные!
         "memory": <MemoryPlugin>,
       }

    Любой ключ может отсутствовать (тогда оркестратор использует fallback).
    Если файл вообще не существует — возвращается пустой dict (полностью
    legacy режим).
    """
    p = Path(config_path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PluginError(f"Invalid YAML in {p}: {e}") from e
    if not isinstance(raw, dict):
        raise PluginError(f"{p}: top-level must be a YAML mapping, got {type(raw).__name__}")

    plugin_configs: dict[str, dict[str, Any]] = raw.get("plugin_config", {}) or {}
    result: dict[str, Any] = {}

    for slot_singular, key in (
        ("runtime", "runtime"),
        ("tracker", "tracker"),
        ("scm", "scm"),
        ("memory", "memory"),
    ):
        name = raw.get(key)
        if not name:
            continue
        cfg = plugin_configs.get(name, {}) or {}
        result[key] = load_plugin(slot_singular, name, config=cfg)

    notifier_names = raw.get("notifiers", []) or []
    if isinstance(notifier_names, str):
        notifier_names = [notifier_names]
    notifiers = []
    for n in notifier_names:
        cfg = plugin_configs.get(n, {}) or {}
        notifiers.append(load_plugin("notifier", n, config=cfg))
    if notifiers:
        result["notifiers"] = notifiers

    return result


def _expand_env_vars(config: dict[str, Any]) -> dict[str, Any]:
    """Заменить ``${VAR}`` в значениях конфига на содержимое env.

    Поддерживает только str-значения верхнего уровня. Для вложенных
    структур значения остаются как есть.
    """
    out: dict[str, Any] = {}
    for k, v in config.items():
        if isinstance(v, str) and "${" in v:
            out[k] = os.path.expandvars(v)
        else:
            out[k] = v
    return out


__all__ = [
    "SLOTS",
    "PluginError",
    "PluginNotFound",
    "PluginLoadError",
    "load_plugin",
    "load_from_config",
    "registered_plugins",
]
