"""Graph tests: RAG path, tool path, and node routing - all with FakeProvider."""

from __future__ import annotations

from uuid import uuid4

import pytest

from common.llm import FakeProvider
from agent_runtime.graph import run_agent


async def _fake_retrieve(agent_id, question):
    return ["The capital of France is Paris.", "Paris is known for the Eiffel Tower."]


@pytest.mark.asyncio
async def test_rag_path_uses_context():
    result = await run_agent(
        agent_id=uuid4(),
        question="What is the capital of France?",
        provider=FakeProvider(),
        retrieve_fn=_fake_retrieve,
        tool_names=[],
    )
    assert result["answer"].startswith("[fake-llm]")
    # Sources come from the retriever node.
    assert len(result["sources"]) == 2


@pytest.mark.asyncio
async def test_tool_path_runs_calculator():
    result = await run_agent(
        agent_id=uuid4(),
        question="What is 21 * 2?",
        provider=FakeProvider(),
        retrieve_fn=_fake_retrieve,
        tool_names=["calculator"],
    )
    assert "42" in result["answer"]
    # Tool path does not use RAG sources.
    assert result["sources"] == []


@pytest.mark.asyncio
async def test_tool_not_used_without_calculator_tool():
    # Arithmetic question but calculator NOT attached -> falls back to RAG.
    result = await run_agent(
        agent_id=uuid4(),
        question="What is 21 * 2?",
        provider=FakeProvider(),
        retrieve_fn=_fake_retrieve,
        tool_names=[],
    )
    assert result["answer"].startswith("[fake-llm]")


@pytest.mark.asyncio
async def test_tool_path_runs_word_count():
    # "how many words" triggers the word_count route in the supervisor.
    result = await run_agent(
        agent_id=uuid4(),
        question="How many words are in this sentence",
        provider=FakeProvider(),
        retrieve_fn=_fake_retrieve,
        tool_names=["word_count"],
    )
    # "How many words are in this sentence" = 7 words.
    assert "7" in result["answer"]
    assert result["sources"] == []


@pytest.mark.asyncio
async def test_word_count_not_used_without_tool():
    # Same question but the tool is NOT attached -> falls back to RAG.
    result = await run_agent(
        agent_id=uuid4(),
        question="How many words are in this sentence",
        provider=FakeProvider(),
        retrieve_fn=_fake_retrieve,
        tool_names=[],
    )
    assert result["answer"].startswith("[fake-llm]")
