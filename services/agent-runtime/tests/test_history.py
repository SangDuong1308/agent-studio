"""Conversation memory in the graph: follow-ups are rewritten into standalone
search queries, and the writer sees the earlier turns.

``RecordingProvider`` is a scripted stand-in for the model: it records every call
and answers the rewrite request and the final answer differently, so each test can
see exactly what was searched and what the writer was given.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from agent_runtime.graph import REWRITE_SYSTEM, SYSTEM_RULES, run_agent
from common.llm import FakeProvider

HISTORY = [
    {"role": "user", "content": "What is the refund window?"},
    {"role": "agent", "content": "Refunds are accepted within 30 days."},
]


class RecordingProvider(FakeProvider):
    def __init__(self, rewrite="refund window for digital products", fail_rewrite=False):
        self.rewrite = rewrite
        self.fail_rewrite = fail_rewrite
        self.rewrite_calls: list[str] = []
        self.writer_calls: list[tuple[str, str]] = []  # (prompt, system)

    async def chat(self, prompt, *, system=None):
        if system == REWRITE_SYSTEM:
            self.rewrite_calls.append(prompt)
            if self.fail_rewrite:
                raise RuntimeError("model unavailable")
            return self.rewrite
        self.writer_calls.append((prompt, system or ""))
        return "the answer"


class Searches:
    """A retrieve function that remembers what it was asked to search for."""

    def __init__(self):
        self.queries: list[str] = []

    async def __call__(self, agent_id, query):
        self.queries.append(query)
        return ["Refunds within 30 days."]


async def _run(provider, searches, question, history, tool_names=()):
    return await run_agent(
        agent_id=uuid4(),
        question=question,
        provider=provider,
        retrieve_fn=searches,
        tool_names=list(tool_names),
        history=history,
    )


@pytest.mark.asyncio
async def test_first_question_is_searched_as_is_and_costs_no_rewrite_call():
    provider, searches = RecordingProvider(), Searches()

    await _run(provider, searches, "What is the refund window?", history=[])

    assert searches.queries == ["What is the refund window?"]
    assert provider.rewrite_calls == []  # nothing to resolve, so no extra model call
    assert len(provider.writer_calls) == 1


@pytest.mark.asyncio
async def test_follow_up_is_rewritten_before_searching():
    provider, searches = RecordingProvider(), Searches()

    await _run(provider, searches, "and for digital products?", history=HISTORY)

    assert searches.queries == ["refund window for digital products"]
    # The rewriter was shown the earlier turns and the new message.
    assert "What is the refund window?" in provider.rewrite_calls[0]
    assert "and for digital products?" in provider.rewrite_calls[0]


@pytest.mark.asyncio
async def test_writer_still_answers_the_original_question_with_history():
    provider, searches = RecordingProvider(), Searches()

    await _run(provider, searches, "and for digital products?", history=HISTORY)

    prompt, system = provider.writer_calls[0]
    assert "Question: and for digital products?" in prompt  # not the rewritten query
    assert "<history>" in prompt
    assert "User: What is the refund window?" in prompt
    assert "Assistant: Refunds are accepted within 30 days." in prompt
    assert "<history>" in system  # the rules explain how to use it
    assert "<context>" in prompt  # facts still come from retrieved documents


@pytest.mark.asyncio
async def test_without_history_the_prompt_has_no_history_block():
    provider, searches = RecordingProvider(), Searches()

    await _run(provider, searches, "What is the refund window?", history=[])

    prompt, system = provider.writer_calls[0]
    assert "<history>" not in prompt
    assert system.endswith(SYSTEM_RULES)


@pytest.mark.asyncio
async def test_failed_rewrite_falls_back_to_the_original_question():
    provider, searches = RecordingProvider(fail_rewrite=True), Searches()

    result = await _run(provider, searches, "and for digital products?", history=HISTORY)

    assert searches.queries == ["and for digital products?"]
    assert result["answer"] == "the answer"  # the user still gets an answer


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_rewrite", ["", "   \n", "x" * 501])
async def test_empty_or_runaway_rewrite_falls_back(bad_rewrite):
    provider, searches = RecordingProvider(rewrite=bad_rewrite), Searches()

    await _run(provider, searches, "and for digital products?", history=HISTORY)

    assert searches.queries == ["and for digital products?"]


@pytest.mark.asyncio
async def test_tool_questions_skip_the_rewriter_even_with_history():
    provider, searches = RecordingProvider(), Searches()

    result = await _run(
        provider, searches, "what is 6 * 7?", history=HISTORY, tool_names=["calculator"]
    )

    assert "42" in result["answer"]
    assert provider.rewrite_calls == [] and provider.writer_calls == []
    assert searches.queries == []
