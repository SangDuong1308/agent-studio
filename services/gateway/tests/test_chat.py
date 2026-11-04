"""Chat persistence: every exchange is stored, reloaded in order, and the last few
messages are sent to the runtime as the agent's memory.

The real agent-runtime is replaced by `fake_runtime`, which records what the
gateway sent it, so these tests need no model, no network and no pgvector.
"""

from __future__ import annotations

import uuid

import gateway.main as main_mod
import pytest
from db.models import Agent, AgentStatus, ChatMessage
from fastapi import HTTPException
from gateway.schemas import QueryOut, Source
from sqlalchemy import func, select


async def _register_and_login(client, email="a@example.com", password="password123"):
    r = await client.post(
        "/v1/auth/register", json={"email": email, "password": password}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/auth/login", data={"username": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _active_agent(client, token) -> str:
    r = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "Support Bot",
            "documents": [
                {"filename": "faq.txt", "content": "Refunds within 30 days."}
            ],
            "tool_names": [],
        },
    )
    assert r.status_code == 202, r.text
    agent_id = r.json()["id"]
    async with client.session_maker() as s:  # play the orchestrator
        agent = await s.get(Agent, uuid.UUID(agent_id))
        agent.status = AgentStatus.ACTIVE
        await s.commit()
    return agent_id


async def _count_messages(client) -> int:
    async with client.session_maker() as s:
        return await s.scalar(select(func.count()).select_from(ChatMessage))


@pytest.fixture
def fake_runtime(monkeypatch):
    """Replace the call to agent-runtime. ``calls`` records (question, history)."""
    calls: list[tuple[str, list[dict]]] = []

    async def _fake(agent_id, question, history):
        calls.append((question, history))
        return QueryOut(
            answer=f"echo: {question}",
            sources=[
                Source(filename="faq.txt", chunk_index=0, snippet="Refunds", score=0.9)
            ],
        )

    monkeypatch.setattr(main_mod, "_ask_runtime", _fake)
    return calls


async def _ask(client, token, agent_id, question):
    r = await client.post(
        f"/v1/agents/{agent_id}/query", headers=_auth(token), json={"question": question}
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_new_agent_has_no_messages(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    r = await client.get(f"/v1/agents/{agent_id}/messages", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_exchange_is_stored_and_reloaded_in_order(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    await _ask(client, token, agent_id, "first?")
    await _ask(client, token, agent_id, "second?")

    r = await client.get(f"/v1/agents/{agent_id}/messages", headers=_auth(token))
    messages = r.json()
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "first?"),
        ("agent", "echo: first?"),
        ("user", "second?"),
        ("agent", "echo: second?"),
    ]
    # The retrieved passages are kept, so a reloaded chat looks like the live one.
    assert messages[1]["sources"][0]["filename"] == "faq.txt"
    assert messages[0]["sources"] == []


@pytest.mark.asyncio
async def test_first_question_sends_no_history(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    await _ask(client, token, agent_id, "first?")

    assert fake_runtime == [("first?", [])]


@pytest.mark.asyncio
async def test_follow_up_sends_the_previous_turn_as_history(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    await _ask(client, token, agent_id, "first?")
    await _ask(client, token, agent_id, "and then?")

    question, history = fake_runtime[1]
    assert question == "and then?"
    assert history == [
        {"role": "user", "content": "first?"},
        {"role": "agent", "content": "echo: first?"},
    ]


@pytest.mark.asyncio
async def test_only_the_last_six_messages_are_sent_as_memory(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    for i in range(5):  # 10 stored messages after five exchanges
        await _ask(client, token, agent_id, f"q{i}")
    await _ask(client, token, agent_id, "latest")

    _, history = fake_runtime[-1]
    assert len(history) == main_mod.HISTORY_MESSAGES == 6
    # The newest six: q2..q4 (question + answer each), oldest first.
    assert [m["content"] for m in history] == [
        "q2", "echo: q2", "q3", "echo: q3", "q4", "echo: q4",
    ]
    # ...but nothing was thrown away: all 12 messages are stored.
    assert await _count_messages(client) == 12


@pytest.mark.asyncio
async def test_long_messages_are_trimmed_in_the_memory_not_in_storage(
    client, fake_runtime, monkeypatch
):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)
    long_text = "x" * (main_mod.HISTORY_CHARS + 500)

    async def _long_answer(agent_id, question, history):
        fake_runtime.append((question, history))
        return QueryOut(answer=long_text)

    monkeypatch.setattr(main_mod, "_ask_runtime", _long_answer)
    await _ask(client, token, agent_id, "tell me everything")
    await _ask(client, token, agent_id, "and more?")

    _, history = fake_runtime[-1]
    assert len(history[1]["content"]) == main_mod.HISTORY_CHARS
    r = await client.get(f"/v1/agents/{agent_id}/messages", headers=_auth(token))
    assert r.json()[1]["content"] == long_text  # stored in full


@pytest.mark.asyncio
async def test_failed_runtime_call_stores_nothing(client, monkeypatch):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    async def _boom(agent_id, question, history):
        raise HTTPException(502, "agent-runtime error")

    monkeypatch.setattr(main_mod, "_ask_runtime", _boom)
    r = await client.post(
        f"/v1/agents/{agent_id}/query", headers=_auth(token), json={"question": "hi"}
    )
    assert r.status_code == 502
    # No orphan question in the history that the next call would drag along.
    assert await _count_messages(client) == 0


@pytest.mark.asyncio
async def test_query_on_a_provisioning_agent_is_rejected_and_not_stored(
    client, fake_runtime
):
    token = await _register_and_login(client)
    r = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "Bot", "documents": [{"filename": "a.txt", "content": "x"}]},
    )
    agent_id = r.json()["id"]  # still PROVISIONING: nobody played the orchestrator

    r = await client.post(
        f"/v1/agents/{agent_id}/query", headers=_auth(token), json={"question": "hi"}
    )
    assert r.status_code == 409
    assert fake_runtime == []
    assert await _count_messages(client) == 0


@pytest.mark.asyncio
async def test_clear_chat_deletes_messages_and_resets_memory(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)
    await _ask(client, token, agent_id, "first?")

    r = await client.delete(f"/v1/agents/{agent_id}/messages", headers=_auth(token))
    assert r.status_code == 204
    r = await client.get(f"/v1/agents/{agent_id}/messages", headers=_auth(token))
    assert r.json() == []

    await _ask(client, token, agent_id, "fresh start")
    assert fake_runtime[-1] == ("fresh start", [])


@pytest.mark.asyncio
async def test_clear_chat_only_touches_that_agent(client, fake_runtime):
    token = await _register_and_login(client)
    a = await _active_agent(client, token)
    b = await _active_agent(client, token)
    await _ask(client, token, a, "for a")
    await _ask(client, token, b, "for b")

    await client.delete(f"/v1/agents/{a}/messages", headers=_auth(token))

    r = await client.get(f"/v1/agents/{b}/messages", headers=_auth(token))
    assert [m["content"] for m in r.json()] == ["for b", "echo: for b"]


@pytest.mark.asyncio
async def test_another_user_cannot_read_or_clear_the_chat(client, fake_runtime):
    owner = await _register_and_login(client, "owner@example.com")
    other = await _register_and_login(client, "other@example.com")
    agent_id = await _active_agent(client, owner)
    await _ask(client, owner, agent_id, "private question")

    r = await client.get(f"/v1/agents/{agent_id}/messages", headers=_auth(other))
    assert r.status_code == 404
    r = await client.delete(f"/v1/agents/{agent_id}/messages", headers=_auth(other))
    assert r.status_code == 404
    assert await _count_messages(client) == 2  # untouched


@pytest.mark.asyncio
async def test_messages_require_login(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    assert (await client.get(f"/v1/agents/{agent_id}/messages")).status_code == 401
    assert (await client.delete(f"/v1/agents/{agent_id}/messages")).status_code == 401


@pytest.mark.asyncio
async def test_deleting_the_agent_deletes_its_chat(client, fake_runtime):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)
    await _ask(client, token, agent_id, "first?")
    assert await _count_messages(client) == 2

    r = await client.delete(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert r.status_code == 204
    assert await _count_messages(client) == 0
