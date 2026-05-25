"""Plugin-slot system orchX (P0.2 — см. docs/recommendations.md).

orchX поддерживает **5 plugin slot'ов**:

* ``runtime``  — где и как исполняется worker (local / docker / podman / …).
* ``tracker``  — откуда задачи / куда статусы (github / linear / gitlab / …).
* ``scm``      — где живут ветки и PR'ы (github / gitlab / …).
* ``notifier`` — куда отправлять события (noop / slack / discord / webhook / …).
* ``memory``   — backend для долговременной памяти (noop / sqlite / …).

Дефолтные реализации шиппятся вместе с пакетом и регистрируются через
``[project.entry-points]`` в ``pyproject.toml``. Сторонние пакеты могут
зарегистрировать свои плагины — orchX подхватит их автоматически:

.. code-block:: toml

   # pyproject.toml стороннего пакета:
   [project.entry-points."orchx.runtime"]
   podman = "my_pkg.runtime:PodmanRuntime"

Use:

.. code-block:: python

   from orchx.plugins import load_plugin
   slack = load_plugin("notifier", "slack", config={"webhook_url": "..."})
   await slack.notify("run_started", {"task_id": "T1"})

Or через high-level helper:

.. code-block:: python

   from orchx.plugins import load_from_config
   plugins = load_from_config("/path/to/.orchx/config.yaml")
   # → {"runtime": ..., "tracker": ..., "notifiers": [...], "memory": ...}
"""

from __future__ import annotations

from .contracts import (
    KanbanTrackerPlugin,
    MemoryPlugin,
    NotifierPlugin,
    RuntimePlugin,
    SCMPlugin,
    TaskHandle,
    TrackerPlugin,
    WorkerOutcomeLike,
)
from .registry import load_from_config, load_plugin, registered_plugins

__all__ = [
    "KanbanTrackerPlugin",
    "MemoryPlugin",
    "NotifierPlugin",
    "RuntimePlugin",
    "SCMPlugin",
    "TaskHandle",
    "TrackerPlugin",
    "WorkerOutcomeLike",
    "load_from_config",
    "load_plugin",
    "registered_plugins",
]
