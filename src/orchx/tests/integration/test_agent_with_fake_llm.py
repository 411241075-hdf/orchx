"""End-to-end integration test: run_agent с FakeLLMClient (P0.5).

Цель: убедиться, что весь agent-loop работает без реального LLM Proxy:

1. Agent получает prompt + tools.
2. LLM возвращает scripted tool_call(write_file).
3. Tool вызывается, файл создаётся.
4. LLM возвращает финальный текст (без tool_calls).
5. Цикл завершается, WorkerOutcome корректный.

Этот тест — gold standard regression. Если он падает — что-то фундаментально
сломалось в agent loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.agent.worker import run_agent
from orchx.tests.fixtures.mock_llm import FakeLLMClient, scripted

pytestmark = [pytest.mark.integration]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Готовит worktree с минимальным набором файлов для tool-call'ов."""
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    (tmp_path / ".orchx").mkdir(exist_ok=True)
    return tmp_path


@pytest.mark.asyncio
async def test_agent_writes_file_via_tool_call(workspace: Path):
    """Agent → write tool → файл создаётся → agent завершается."""
    llm = FakeLLMClient(
        scripted(
            [
                {
                    "text": "I'll create the file.",
                    "tool_calls": [
                        {
                            "name": "write",
                            "args": {
                                "file_path": "new.txt",
                                "content": "hello world\n",
                            },
                        }
                    ],
                },
                {"text": "Done.", "tool_calls": []},
            ]
        )
    )

    outcome = await run_agent(
        role="implementer",
        cwd=workspace,
        repo_root=workspace,
        user_prompt="Create new.txt with 'hello world'",
        llm=llm,
        effort=None,
        timeout_s=30,
        log_file=workspace / ".orchx" / "agent.log",
    )

    # Worker завершился успешно.
    assert outcome.returncode == 0, f"returncode={outcome.returncode}, stderr={outcome.stderr}"
    assert not outcome.timed_out
    # Файл создан.
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "hello world\n"
    # Agent сделал 2 LLM-вызова (один с tool_call, один финальный).
    assert outcome.llm_calls == 2
    # Сумма токенов сложилась.
    assert outcome.input_tokens > 0
    assert outcome.output_tokens > 0


@pytest.mark.asyncio
async def test_agent_finishes_without_tool_calls(workspace: Path):
    """Если LLM сразу не зовёт tool'ы — agent завершается за один шаг."""
    llm = FakeLLMClient(
        scripted(
            [
                {"text": "Already done, nothing to do.", "tool_calls": []},
            ]
        )
    )
    outcome = await run_agent(
        role="implementer",
        cwd=workspace,
        repo_root=workspace,
        user_prompt="Verify the system",
        llm=llm,
        effort=None,
        timeout_s=10,
        log_file=workspace / ".orchx" / "agent.log",
    )
    assert outcome.returncode == 0
    assert outcome.llm_calls == 1


@pytest.mark.asyncio
async def test_agent_handles_permission_denied_gracefully(workspace: Path):
    """Запрещённый tool (например, bash) возвращает ошибку, agent не падает."""
    llm = FakeLLMClient(
        scripted(
            [
                {
                    "text": "Trying bash...",
                    "tool_calls": [
                        {
                            "name": "bash",
                            "args": {"command": "rm -rf /"},
                        }
                    ],
                },
                {"text": "Got denied, finishing.", "tool_calls": []},
            ]
        )
    )
    outcome = await run_agent(
        role="implementer",  # implementer обычно НЕ имеет bash
        cwd=workspace,
        repo_root=workspace,
        user_prompt="Try to be evil",
        llm=llm,
        effort=None,
        timeout_s=10,
        log_file=workspace / ".orchx" / "agent.log",
    )
    # Цикл завершился штатно — bash был отвергнут permissions, но agent не упал.
    assert outcome.returncode == 0
