"""Docker runtime plugin (P1.2).

Запускает orchX-worker'а в изолированном Docker-контейнере вместо
in-process asyncio. Даёт:

* Изоляцию от malicious кода (network=none, read-only repo mount).
* Reproducible-окружение (тот же Python и тот же набор tool'ов).
* Чистый rollback (контейнер удаляется после).

**Состояние реализации (P1.2):**

* Контракт совместим с :class:`orchx.plugins.contracts.RuntimePlugin`.
* Использует ``docker`` Python SDK (extras ``orchx[docker]``).
* Image сборка: см. ``src/orchx/templates/runtime/Dockerfile.worker``.
  Пользователь собирает image один раз: ``docker build -f Dockerfile.worker -t orchx-worker:latest .``
* Передача prompt'а в контейнер — через файл в worktree mount'е (worktree
  и так монтируется RW).

Известные ограничения этой версии:

* ``llm``-инстанс не передаётся в контейнер (контейнер сам поднимает LLM
  клиент по env). orchestrator должен пробросить нужные env (``OPENAI_BASE_URL``,
  ``OPENAI_API_KEY``, ``ORCHX_*_MODEL`` и т.п.) через ``env=``.
* on_activity tail парсится из ``docker logs`` follow.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from pathlib import Path
from typing import Any

from ...agent.worker import WorkerOutcome


class DockerRuntime:
    """Запуск worker'а в Docker-контейнере.

    Config:
        image: имя образа (default: ``orchx-worker:latest``).
        network: docker network mode (default: ``none`` — без сети).
        cpu_quota: например ``"2"`` или ``"0.5"`` (cgroup quota).
        memory: например ``"2g"``.
        env_passthrough: список env-переменных для проброса в контейнер.
        extra_args: список дополнительных аргументов для ``docker run``.
    """

    name = "docker"

    def __init__(
        self,
        *,
        image: str = "orchx-worker:latest",
        network: str = "none",
        cpu_quota: str | None = None,
        memory: str | None = None,
        env_passthrough: list[str] | None = None,
        extra_args: list[str] | None = None,
        **_: Any,
    ) -> None:
        self.image = image
        self.network = network
        self.cpu_quota = cpu_quota
        self.memory = memory
        self.env_passthrough = env_passthrough or [
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "ORCHX_LLM_BASE_URL",
            "ORCHX_LLM_API_KEY",
            "ORCHX_LLM_MODEL",
            "ORCHX_PLANNER_MODEL",
            "ORCHX_REVIEWER_MODEL",
            "ORCHX_DEBUGGER_MODEL",
            "ORCHX_MERGER_MODEL",
            "ORCHX_CONTEXT_WINDOW",
        ]
        self.extra_args = extra_args or []

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
        llm: Any = None,  # noqa: ARG002 — Docker runtime использует env
    ) -> WorkerOutcome:
        """Запустить ``docker run`` с командой ``orchx-internal-worker``."""
        # Записываем prompt в worktree (mount'ится RW), worker читает его.
        prompt_file = cwd / ".orchx" / "docker-prompt.txt"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")

        container_workspace = "/workspace"
        container_repo = "/repo"
        container_prompt_path = f"{container_workspace}/.orchx/docker-prompt.txt"

        cmd: list[str] = [
            "docker",
            "run",
            "--rm",
            "--network",
            self.network,
            "--cap-drop=ALL",
            "-v",
            f"{cwd.resolve()}:{container_workspace}:rw",
            "-v",
            f"{repo_root.resolve()}:{container_repo}:ro",
            "-w",
            container_workspace,
        ]
        if self.cpu_quota:
            cmd.extend(["--cpus", self.cpu_quota])
        if self.memory:
            cmd.extend(["--memory", self.memory])
        for env_name in self.env_passthrough:
            val = os.environ.get(env_name)
            if val is not None:
                cmd.extend(["-e", f"{env_name}={val}"])
        cmd.extend(["-e", f"ORCHX_DOCKER_ROLE={role}"])
        if effort:
            cmd.extend(["-e", f"ORCHX_DOCKER_EFFORT={effort}"])
        cmd.extend(self.extra_args)
        cmd.append(self.image)
        # Entrypoint в Dockerfile запускает `python -m orchx.agent.worker`
        # с этими аргументами.
        cmd.extend(
            [
                "--role",
                role,
                "--prompt-file",
                container_prompt_path,
                "--timeout",
                str(int(timeout_s)),
            ]
        )

        started = time.monotonic()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"\n[docker-runtime] $ {shlex.join(cmd)}\n")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "docker CLI not found. Install Docker or set runtime: local in config."
            ) from e

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s + 30
            )
            timed_out = False
        except TimeoutError:
            proc.kill()
            await proc.wait()
            stdout_b, stderr_b = b"", b"[docker-runtime] timed out"
            timed_out = True

        elapsed = time.monotonic() - started
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(stdout)
            lf.write(stderr)

        # Контейнер по контракту пишет тут JSON-summary с input/output_tokens.
        metrics_file = cwd / ".orchx" / "docker-metrics.json"
        input_tokens = output_tokens = llm_calls = compactions = 0
        cost_usd = 0.0
        if metrics_file.exists():
            try:
                m = json.loads(metrics_file.read_text(encoding="utf-8"))
                input_tokens = int(m.get("input_tokens", 0))
                output_tokens = int(m.get("output_tokens", 0))
                llm_calls = int(m.get("llm_calls", 0))
                compactions = int(m.get("compactions", 0))
                cost_usd = float(m.get("cost_usd", 0.0))
            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        if on_activity and stdout:
            try:
                on_activity(stdout.splitlines()[-1] if stdout.splitlines() else "")
            except Exception:  # noqa: BLE001
                pass

        return WorkerOutcome(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_s=elapsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            llm_calls=llm_calls,
            compactions=compactions,
            cost_usd=cost_usd,
        )


__all__ = ["DockerRuntime"]
