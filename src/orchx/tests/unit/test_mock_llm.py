"""Smoke-тесты FakeLLMClient (P0.5)."""

from __future__ import annotations

import pytest

from orchx.tests.fixtures.mock_llm import FakeLLMClient, ScriptedResponse, scripted


@pytest.mark.asyncio
async def test_fake_llm_returns_scripted_responses():
    llm = FakeLLMClient(
        scripted(
            [
                {"text": "step 1"},
                {"text": "step 2", "tool_calls": [{"name": "write", "args": {"x": 1}}]},
                {"text": "done"},
            ]
        )
    )
    r1 = await llm.chat(messages=[])
    r2 = await llm.chat(messages=[])
    r3 = await llm.chat(messages=[])
    assert r1.text == "step 1"
    assert r2.text == "step 2"
    assert len(r2.tool_calls) == 1
    assert r2.tool_calls[0].name == "write"
    assert r2.tool_calls[0].arguments == {"x": 1}
    assert r3.text == "done"


@pytest.mark.asyncio
async def test_fake_llm_loops_last_response():
    llm = FakeLLMClient(scripted([{"text": "only"}]), loop=True)
    r1 = await llm.chat(messages=[])
    r2 = await llm.chat(messages=[])
    r3 = await llm.chat(messages=[])
    assert r1.text == r2.text == r3.text == "only"


@pytest.mark.asyncio
async def test_fake_llm_no_loop_raises_after_exhaust():
    llm = FakeLLMClient(scripted([{"text": "a"}, {"text": "b"}]), loop=False)
    await llm.chat(messages=[])
    await llm.chat(messages=[])
    with pytest.raises(RuntimeError, match="exhausted"):
        await llm.chat(messages=[])


@pytest.mark.asyncio
async def test_fake_llm_for_role_inherits_responses():
    llm = FakeLLMClient(scripted([{"text": "x"}]))
    child = llm.for_role("planner", effort="xhigh")
    assert child.effort == "xhigh"
    r = await child.chat(messages=[])
    assert r.text == "x"


@pytest.mark.asyncio
async def test_fake_llm_records_calls():
    llm = FakeLLMClient(scripted([{"text": "a"}, {"text": "b"}]))
    await llm.chat(messages=[{"role": "user", "content": "hi"}])
    await llm.chat(messages=[{"role": "user", "content": "hello"}])
    assert len(llm.calls) == 2
    assert llm.calls[0]["messages"][0]["content"] == "hi"


@pytest.mark.asyncio
async def test_fake_llm_text_delta_callback_invoked():
    seen: list[str] = []

    def cb(d: str) -> None:
        seen.append(d)

    llm = FakeLLMClient(scripted([{"text": "hello"}]))
    await llm.chat(messages=[], on_text_delta=cb)
    assert seen == ["hello"]


@pytest.mark.asyncio
async def test_scripted_response_defaults():
    r = ScriptedResponse()
    assert r.text == ""
    assert r.tool_calls == []
    assert r.input_tokens == 100
    assert r.output_tokens == 50
