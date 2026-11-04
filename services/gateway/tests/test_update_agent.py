"""Editing an agent: PATCH /v1/agents/{id} and the richer GET /v1/agents/{id}.

The rule being tested: changes that need no embedding (name, description, tools,
removing documents) apply instantly; adding documents starts a saga in `update`
mode. In these tests the "orchestrator" is played by `_activate`, which flips the
agent to ACTIVE the way the real saga would when provisioning succeeds.
"""

from __future__ import annotations

import uuid

import pytest
from db.models import Agent, AgentStatus, Document, SagaState, SagaStatus, Tool
from sqlalchemy import select


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


async def _create(client, token, **overrides) -> str:
    body = {
        "name": "Support Bot",
        "description": "v1",
        "documents": [{"filename": "faq.txt", "content": "Refunds within 30 days."}],
        "tool_names": ["calculator"],
        **overrides,
    }
    r = await client.post("/v1/agents", headers=_auth(token), json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def _activate(client, agent_id: str) -> None:
    """Pretend the provisioning saga finished."""
    async with client.session_maker() as s:
        agent = await s.get(Agent, uuid.UUID(agent_id))
        agent.status = AgentStatus.ACTIVE
        await s.commit()


async def _active_agent(client, token, **overrides) -> str:
    agent_id = await _create(client, token, **overrides)
    await _activate(client, agent_id)
    client.fake_broker.published.clear()
    return agent_id


@pytest.mark.asyncio
async def test_get_agent_returns_documents_and_tools(client):
    token = await _register_and_login(client)
    agent_id = await _create(client, token, tool_names=["calculator", "word_count"])

    r = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tool_names"] == ["calculator", "word_count"]
    assert [d["filename"] for d in body["documents"]] == ["faq.txt"]
    assert body["documents"][0]["characters"] == len("Refunds within 30 days.")
    assert "content" not in body["documents"][0]  # the text itself is not returned
    assert body["last_saga_status"] == "RUNNING"


@pytest.mark.asyncio
async def test_edit_name_description_tools_is_instant(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    r = await client.patch(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"name": "Renamed", "description": "v2", "tool_names": ["word_count"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "v2"
    assert body["tool_names"] == ["word_count"]  # calculator removed, word_count added
    assert body["status"] == "ACTIVE"  # no saga, no downtime
    assert client.fake_broker.published == []  # nothing to ingest, nothing published


@pytest.mark.asyncio
async def test_edit_leaves_unmentioned_fields_alone(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    r = await client.patch(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"description": "only this"}
    )
    body = r.json()
    assert body["description"] == "only this"
    assert body["name"] == "Support Bot"
    assert body["tool_names"] == ["calculator"]
    assert len(body["documents"]) == 1


@pytest.mark.asyncio
async def test_empty_tool_list_removes_all_tools(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    r = await client.patch(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"tool_names": []}
    )
    assert r.status_code == 200
    assert r.json()["tool_names"] == []


@pytest.mark.asyncio
async def test_remove_document_is_instant(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)
    doc_id = (await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))).json()[
        "documents"
    ][0]["id"]

    r = await client.patch(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"remove_document_ids": [doc_id]},
    )
    assert r.status_code == 200
    assert r.json()["documents"] == []
    assert r.json()["status"] == "ACTIVE"
    assert client.fake_broker.published == []


@pytest.mark.asyncio
async def test_add_document_starts_an_update_saga(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    r = await client.patch(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={
            "add_documents": [
                {"filename": "shipping.txt", "content": "Ships in 2 days."}
            ]
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "PROVISIONING"  # re-ingesting: chat is paused
    assert {d["filename"] for d in body["documents"]} == {"faq.txt", "shipping.txt"}

    # Exactly one command, asking to ingest only the NEW document.
    assert len(client.fake_broker.published) == 1
    _, message = client.fake_broker.published[0]
    assert len(message.document_ids) == 1

    async with client.session_maker() as s:
        saga = await s.scalar(
            select(SagaState).where(SagaState.id == message.saga_id)
        )
        assert saga.status == SagaStatus.RUNNING
        assert saga.payload["kind"] == "update"
        assert saga.payload["previous_status"] == "ACTIVE"
        assert saga.payload["new_document_ids"] == [str(message.document_ids[0])]
        assert saga.pending_documents == [str(message.document_ids[0])]


@pytest.mark.asyncio
async def test_message_carries_the_request_correlation_id(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    await client.patch(
        f"/v1/agents/{agent_id}",
        headers={**_auth(token), "X-Correlation-ID": "trace-me-123"},
        json={"add_documents": [{"filename": "a.txt", "content": "x"}]},
    )
    _, message = client.fake_broker.published[0]
    assert message.correlation_id == "trace-me-123"


@pytest.mark.asyncio
async def test_cannot_edit_while_provisioning(client):
    token = await _register_and_login(client)
    agent_id = await _create(client, token)  # still PROVISIONING

    r = await client.patch(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"name": "nope"}
    )
    assert r.status_code == 409
    assert "PROVISIONING" in r.json()["detail"]


@pytest.mark.asyncio
async def test_cannot_edit_a_failed_agent(client):
    token = await _register_and_login(client)
    agent_id = await _create(client, token)
    async with client.session_maker() as s:
        agent = await s.get(Agent, uuid.UUID(agent_id))
        agent.status = AgentStatus.FAILED
        await s.commit()

    r = await client.patch(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"name": "nope"}
    )
    assert r.status_code == 409
    assert "delete it" in r.json()["detail"]


@pytest.mark.asyncio
async def test_second_edit_is_blocked_until_the_saga_finishes(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)
    add = {"add_documents": [{"filename": "a.txt", "content": "x"}]}

    url = f"/v1/agents/{agent_id}"
    first = await client.patch(url, headers=_auth(token), json=add)
    second = await client.patch(url, headers=_auth(token), json=add)
    assert first.status_code == 202
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_other_users_agent_is_404(client):
    owner = await _register_and_login(client, email="owner@example.com")
    stranger = await _register_and_login(client, email="stranger@example.com")
    agent_id = await _active_agent(client, owner)

    r = await client.patch(
        f"/v1/agents/{agent_id}", headers=_auth(stranger), json={"name": "mine now"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edit_requires_auth(client):
    r = await client.patch(f"/v1/agents/{uuid.uuid4()}", json={"name": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edit_validation(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)
    url = f"/v1/agents/{agent_id}"

    # nothing to change
    assert (await client.patch(url, headers=_auth(token), json={})).status_code == 422
    # unknown tool
    r = await client.patch(url, headers=_auth(token), json={"tool_names": ["rm_rf"]})
    assert r.status_code == 422
    # blank name
    r = await client.patch(url, headers=_auth(token), json={"name": ""})
    assert r.status_code == 422
    # document id that is not this agent's
    r = await client.patch(
        url, headers=_auth(token), json={"remove_document_ids": [str(uuid.uuid4())]}
    )
    assert r.status_code == 422
    assert "unknown document" in r.json()["detail"]


@pytest.mark.asyncio
async def test_document_limit_counts_existing_documents(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)  # already has 1 document

    # 1 existing + 10 new = 11, over the limit of 10
    too_many = [{"filename": f"{i}.txt", "content": "x"} for i in range(10)]
    r = await client.patch(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"add_documents": too_many}
    )
    assert r.status_code == 422
    assert "at most" in r.json()["detail"]


@pytest.mark.asyncio
async def test_broker_down_rolls_back_only_the_new_document(client):
    token = await _register_and_login(client)
    agent_id = await _active_agent(client, token)

    async def _broken_publish(routing_key, message):
        raise ConnectionError("rabbitmq is down")

    client.fake_broker.publish = _broken_publish  # type: ignore[method-assign]

    r = await client.patch(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={
            "name": "Renamed anyway",
            "add_documents": [{"filename": "new.txt", "content": "x"}],
        },
    )
    assert r.status_code == 503

    async with client.session_maker() as s:
        agent = await s.get(Agent, uuid.UUID(agent_id))
        assert agent.status == AgentStatus.ACTIVE  # restored, not stuck PROVISIONING
        names = list(
            await s.scalars(
                select(Document.filename).where(Document.agent_id == agent.id)
            )
        )
        assert names == ["faq.txt"]  # the new document was removed
        tools = list(await s.scalars(select(Tool.name).where(Tool.agent_id == agent.id)))
        assert tools == ["calculator"]
        saga = await s.scalar(
            select(SagaState)
            .where(SagaState.agent_id == agent.id)
            .order_by(SagaState.created_at.desc())
            .limit(1)
        )
        assert saga.status == SagaStatus.FAILED
