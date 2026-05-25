"""Local runtime: воркер исполняется в той же Python-сессии через asyncio.

Это дефолтный runtime и в нём же исполняется legacy-путь через
:func:`orchx.runner.run_worker`. Цель этого plugin'а — дать единый
интерфейс для orchestrator'а, который позже можно будет заменить на
docker / podman / kubernetes без изменения вызывающего кода.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ... import runner


class LocalRuntime:
    """asyncio-based runtime: запуск worker'а напрямую через :mod:`orchx.runner`.

    Никаких дополнительных конфигов не принимает. Поведение полностью
    эквивалентно тому, как работал orchX до P0.2.
    """

    name = "local"

    def __init__(self, **_: Any) -> None:
        # local runtime бесконфигурационен
        pass

    async def spawn_worker(
        self,
        *,
        cwd: Path,
        repo_root: Path,
        role: str,
        prompt: str,
        timeout_s: float,
        log_file: Path,
        effort: str | None,
        on_activity: Any = None,
        llm: Any = None,
    ) -> runner.WorkerOutcome:
        """Запустить worker через :func:`orchx.runner.run_worker`.

        Параметр ``llm`` обязателен (передаётся orchestrator'ом). Сделан
        keyword-only с default-None чтобы Protocol-сигнатура была
        совместима с другими runtime'ами.
        """
        if llm is None:
            raise ValueError("LocalRuntime.spawn_worker requires llm=... kwarg")
        return await runner.run_worker(
            llm=llm,
            cwd=cwd,
            repo_root=repo_root,
            role=role,
            prompt=prompt,
            timeout_s=timeout_s,
            log_file=log_file,
            effort=effort,
            on_activity=on_activity,
        )


__all__ = ["LocalRuntime"]
