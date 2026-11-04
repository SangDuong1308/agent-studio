"""Gateway API tests: auth flow, authorization, CRUD, pagination."""

from __future__ import annotations

import pytest


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


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_register_duplicate_conflicts(client):
    await _register_and_login(client)
    r = await client.post(
        "/v1/auth/register", json={"email": "a@example.com", "password": "password123"}
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    await _register_and_login(client)
    r = await client.post(
        "/v1/auth/login", data={"username": "a@example.com", "password": "wrong"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_agents_require_auth(client):
    r = await client.get("/v1/agents")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_agent_returns_202_and_publishes(client):
    token = await _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/v1/agents",
        headers=headers,
        json={
            "name": "Support Bot",
            "description": "Answers support questions",
            "documents": [{"filename": "faq.txt", "content": "Hello world"}],
            "tool_names": ["calculator"],
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "PROVISIONING"
    # A provisioning command was published to the (fake) broker.
    assert len(client.fake_broker.published) == 1


@pytest.mark.asyncio
async def test_list_pagination(client):
    token = await _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(3):
        await client.post(
            "/v1/agents", headers=headers, json={"name": f"agent-{i}"}
        )
    r = await client.get("/v1/agents?limit=2&offset=0", headers=headers)
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2


@pytest.mark.asyncio
async def test_validation_error_returns_422(client):
    token = await _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/v1/agents", headers=headers, json={"name": ""})
    assert r.status_code == 422
