"""Тест agent loop'а воркера с моковым LLM-клиентом.

LLM-клиент моки́руется duck-typed классом ``ScriptedLLM`` — он реализует
тот же интерфейс, что и ``orchx.agent.llm.LLMClient`` (методы ``for_role``,
``chat``, свойства ``model``, ``effort``, ``base_url``), но возвращает
заранее скриптованную последовательность ответов.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchx.agent.llm import ChatResponse, ToolCall
from orchx.agent.worker import run_agent

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ScriptedLLM:
    """Дублирует public-интерфейс ``LLMClient``, отдаёт жёстко скриптованные ответы."""

    responses: list[ChatResponse]
    model: str = "scripted/test"
    base_url: str = "scripted://"
    effort: str | None = None
    _iter: Iterator[ChatResponse] = field(init=False)
    last_messages: list[dict] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._iter = iter(self.responses)

    def for_role(
        self, role: str, *, effort: str | None = None
    ) -> ScriptedLLM:  # noqa: ARG002
        return self

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        on_text_delta: Any = None,
        on_tool_call_delta: Any = None,
    ) -> ChatResponse:
        self.last_messages = list(messages)
        try:
            resp = next(self._iter)
        except StopIteration as e:
            raise AssertionError("ScriptedLLM ran out of responses") from e
        if on_text_delta and resp.text:
            try:
                on_text_delta(resp.text)
            except Exception:  # noqa: BLE001
                pass
        for tc in resp.tool_calls:
            if on_tool_call_delta:
                try:
                    on_tool_call_delta(tc.name)
                except Exception:  # noqa: BLE001
                    pass
        return resp


def _tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=args,
        raw={
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        },
    )


async def test_worker_finishes_when_no_tool_calls(tmp_path: Path) -> None:
    """Если первая же реплика без tool_calls — воркер сразу завершается со 0."""
    llm = ScriptedLLM(responses=[ChatResponse(text="done", tool_calls=[])])
    log = tmp_path / "log.txt"
    outcome = await run_agent(
        role="architect",  # любая роль, file должен существовать
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        user_prompt="trivial",
        llm=llm,  # type: ignore[arg-type]
        timeout_s=10,
        log_file=log,
    )
    assert outcome.returncode == 0
    assert outcome.timed_out is False
    assert "done" in outcome.stdout
    assert log.exists()


async def test_worker_runs_write_tool_then_finishes(tmp_path: Path) -> None:
    """Сценарий: LLM пишет файл через ``write``, затем завершает."""
    llm = ScriptedLLM(
        responses=[
            ChatResponse(
                text="",
                tool_calls=[
                    _tool_call(
                        "write",
                        {"file_path": "result.txt", "content": "hello"},
                    )
                ],
                tool_calls_raw=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": '{"file_path":"result.txt","content":"hello"}',
                        },
                    }
                ],
            ),
            ChatResponse(text="done", tool_calls=[]),
        ]
    )
    log = tmp_path / "log.txt"
    outcome = await run_agent(
        role="architect",
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        user_prompt="write result.txt",
        llm=llm,  # type: ignore[arg-type]
        timeout_s=10,
        log_file=log,
    )
    assert outcome.returncode == 0
    assert (tmp_path / "result.txt").read_text() == "hello"


async def test_worker_exhausts_max_steps(tmp_path: Path) -> None:
    """Если LLM никогда не выходит из tool-цикла — exit 125 (max_steps)."""
    # Architect разрешает edit (allow). Многократно дёргаем write,
    # никогда не завершаем без tool_calls.
    loops = 200  # больше, чем max_steps в architect (60).
    responses = []
    for i in range(loops):
        responses.append(
            ChatResponse(
                text="",
                tool_calls=[
                    _tool_call(
                        "write",
                        {"file_path": f"step{i}.txt", "content": str(i)},
                        call_id=f"c{i}",
                    )
                ],
                tool_calls_raw=[
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": f'{{"file_path":"step{i}.txt","content":"{i}"}}',
                        },
                    }
                ],
            )
        )
    llm = ScriptedLLM(responses=responses)
    log = tmp_path / "log.txt"
    outcome = await run_agent(
        role="architect",
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        user_prompt="loop",
        llm=llm,  # type: ignore[arg-type]
        timeout_s=60,
        log_file=log,
    )
    assert outcome.returncode == 125
    assert outcome.timed_out is False


async def test_worker_unknown_tool_returns_error_message(tmp_path: Path) -> None:
    """Если LLM зовёт несуществующий tool — отвечаем сообщением об ошибке и крутимся дальше."""
    llm = ScriptedLLM(
        responses=[
            ChatResponse(
                text="",
                tool_calls=[_tool_call("nonexistent_tool", {})],
                tool_calls_raw=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "nonexistent_tool", "arguments": "{}"},
                    }
                ],
            ),
            ChatResponse(text="done", tool_calls=[]),
        ]
    )
    log = tmp_path / "log.txt"
    outcome = await run_agent(
        role="architect",
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        user_prompt="trigger unknown tool",
        llm=llm,  # type: ignore[arg-type]
        timeout_s=10,
        log_file=log,
    )
    assert outcome.returncode == 0
    # В сообщениях должна быть ошибка про unknown tool.
    found = any(
        m.get("role") == "tool" and "Unknown tool" in (m.get("content") or "")
        for m in llm.last_messages
    )
    assert found
