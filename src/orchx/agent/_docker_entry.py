"""Internal entry-point для исполнения worker'а внутри Docker-контейнера (P1.2).

Используется :class:`orchx.plugins.runtimes.docker.DockerRuntime`. НЕ
предназначен для прямого вызова пользователем.

Lifecycle внутри контейнера:

1. Читаем prompt из ``--prompt-file``.
2. Поднимаем :class:`orchx.agent.llm.LLMClient` из env-переменных.
3. Запускаем :func:`orchx.agent.worker.run_agent`.
4. Сохраняем метрики в ``.orchx/docker-metrics.json`` (рядом с prompt'ом).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .llm import LLMClient, LLMConfig
from .worker import run_agent


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="orchx.agent._docker_entry",
        description="Internal worker entry inside Docker container",
    )
    p.add_argument("--role", required=True, help="Role short name (implementer/...)")
    p.add_argument(
        "--prompt-file", required=True, type=Path, help="Path to file with user prompt"
    )
    p.add_argument("--timeout", required=True, type=int, help="Wall timeout (seconds)")
    return p.parse_args()


async def _amain() -> int:
    args = _parse_args()
    if not args.prompt_file.is_file():
        sys.stderr.write(
            f"[docker-entry] prompt file not found: {args.prompt_file}\n"
        )
        return 2

    prompt = args.prompt_file.read_text(encoding="utf-8")
    effort = os.environ.get("ORCHX_DOCKER_EFFORT") or None

    cwd = Path.cwd()
    log_file = cwd / ".orchx" / "docker-worker.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cfg = LLMConfig.from_env()
    llm = LLMClient(cfg)

    outcome = await run_agent(
        role=args.role,
        cwd=cwd,
        repo_root=cwd,
        user_prompt=prompt,
        llm=llm,
        effort=effort,
        timeout_s=args.timeout,
        log_file=log_file,
        on_activity=None,
    )

    metrics_file = cwd / ".orchx" / "docker-metrics.json"
    metrics_file.write_text(
        json.dumps(
            {
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "llm_calls": outcome.llm_calls,
                "compactions": outcome.compactions,
                "cost_usd": outcome.cost_usd,
                "duration_s": outcome.duration_s,
                "returncode": outcome.returncode,
                "timed_out": outcome.timed_out,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return outcome.returncode


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
