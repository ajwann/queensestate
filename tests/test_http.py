"""The HTTP transport end to end: OAuth discovery, the gate, and a real tool call."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.applications import Starlette
from test_oauth import ALLOWED, StubResolver, google_config

from queensestate.config import QUEENSESTATE_SCOPE, ConfigError, HttpConfig
from queensestate.http import create_http_app
from queensestate.oauth import GoogleIdentity
from queensestate.token_store import MemoryTokenStore

pytestmark = pytest.mark.anyio

PUBLIC_URL = "https://queensestate.test"

#: Every tool the server exposes; the gate must open onto all of them.
EXPECTED_TOOLS = {
    "get_address_profile",
    "lookup_address",
    "get_trash_and_recycling_schedule",
    "get_crime_near",
    "summarize_crime",
    "get_traffic_crashes_near",
    "get_311_requests_near",
    "get_code_enforcement_cases",
    "get_street_closures",
    "get_capital_projects_near",
    "get_pending_rezonings",
    "find_nearby_places",
    "get_bus_route",
    "get_city_budget",
    "get_city_salary_stats",
    "search_datasets",
    "describe_dataset",
    "query_dataset",
    "summarize_dataset",
}


def http_config(**overrides: Any) -> HttpConfig:
    config = HttpConfig(
        host="127.0.0.1",
        port=8000,
        public_url=PUBLIC_URL,
        mcp_path="/mcp",
        google=google_config(),
    )
    return dataclasses.replace(config, **overrides) if overrides else config


def build_client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL, follow_redirects=False
    )


def build_client(identity: GoogleIdentity = ALLOWED) -> httpx.AsyncClient:
    return build_client_for(create_http_app(http_config(), resolver=StubResolver(identity)))


# -- The gate --------------------------------------------------------------


async def test_the_mcp_endpoint_refuses_an_anonymous_request() -> None:
    async with build_client() as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401
    # The challenge must point at the metadata that tells a client where to sign in.
    assert "resource_metadata" in response.headers["www-authenticate"]


async def test_the_mcp_endpoint_refuses_a_token_it_did_not_issue() -> None:
    async with build_client() as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer not-a-real-token",
            },
        )

    assert response.status_code == 401


async def test_protected_resource_metadata_names_this_server_as_its_own_issuer() -> None:
    async with build_client() as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"].rstrip("/") == f"{PUBLIC_URL}/mcp"
    assert metadata["authorization_servers"] == [PUBLIC_URL]


async def test_authorization_server_metadata_advertises_dynamic_registration() -> None:
    async with build_client() as client:
        response = await client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert metadata["revocation_endpoint"] == f"{PUBLIC_URL}/revoke"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert QUEENSESTATE_SCOPE in metadata["scopes_supported"]


async def test_the_google_callback_rejects_an_unknown_state() -> None:
    async with build_client() as client:
        response = await client.get("/auth/google/callback?code=abc&state=never-issued")

    assert response.status_code == 400
    assert "expired" in response.text


@pytest.mark.parametrize(
    "query",
    ["?error=access_denied", "?code=abc", "?state=abc", ""],
    ids=["user-declined", "no-state", "no-code", "empty"],
)
async def test_the_google_callback_rejects_a_malformed_return(query: str) -> None:
    async with build_client() as client:
        response = await client.get(f"/auth/google/callback{query}")

    assert response.status_code == 400


# -- The whole handshake ---------------------------------------------------

#: PKCE pair for the test client's own leg, precomputed so the flow is fixed.
CODE_VERIFIER = "b" * 43
CODE_CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
CODE_CHALLENGE_VALUE = CODE_CHALLENGE.decode().rstrip("=")

CLIENT_REDIRECT_URI = "http://127.0.0.1:33418/callback"

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/register",
        json={
            "client_name": "test-client",
            "redirect_uris": [CLIENT_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert response.status_code == 201, response.text
    registration: dict[str, str] = response.json()
    return registration


async def _authorize(client: httpx.AsyncClient, client_id: str, **extra: str) -> httpx.Response:
    return await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": CLIENT_REDIRECT_URI,
            "response_type": "code",
            "code_challenge": CODE_CHALLENGE_VALUE,
            "code_challenge_method": "S256",
            "scope": QUEENSESTATE_SCOPE,
            "state": "client-state",
            **extra,
        },
    )


async def _sign_in(client: httpx.AsyncClient, client_id: str) -> str:
    """Drive /authorize and the Google callback, returning the authorization code."""
    authorize = await _authorize(client, client_id, resource=f"{PUBLIC_URL}/mcp")
    assert authorize.status_code == 302, authorize.text
    google_state = parse_qs(urlsplit(authorize.headers["location"]).query)["state"][0]

    callback = await client.get(
        "/auth/google/callback", params={"code": "google-code", "state": google_state}
    )
    assert callback.status_code == 302, callback.text
    returned = parse_qs(urlsplit(callback.headers["location"]).query)
    assert returned["state"] == ["client-state"]
    return returned["code"][0]


async def _redeem(client: httpx.AsyncClient, registration: dict[str, str], code: str) -> str:
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT_URI,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": CODE_VERIFIER,
        },
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def _sse_payload(body: str) -> dict[str, Any]:
    """Pull the JSON out of the single-event SSE response the transport sends."""
    for line in body.splitlines():
        if line.startswith("data: "):
            parsed: dict[str, Any] = json.loads(line.removeprefix("data: "))
            return parsed
    raise AssertionError(f"no SSE data frame in {body!r}")


async def test_a_google_sign_in_yields_a_token_that_opens_the_tools() -> None:
    app = create_http_app(http_config(), resolver=StubResolver(ALLOWED))
    # The streamable-HTTP session manager only runs inside the app's lifespan.
    async with app.router.lifespan_context(app), build_client_for(app) as client:
        registration = await _register(client)
        code = await _sign_in(client, registration["client_id"])
        access_token = await _redeem(client, registration, code)

        auth = {**MCP_HEADERS, "authorization": f"Bearer {access_token}"}
        initialize = await client.post(
            "/mcp",
            headers=auth,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            },
        )
        assert initialize.status_code == 200, initialize.text
        session = initialize.headers["mcp-session-id"]

        session_headers = {**auth, "mcp-session-id": session}
        notified = await client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notified.status_code == 202, notified.text

        listed = await client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200, listed.text

    tools = _sse_payload(listed.text)["result"]["tools"]
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS


async def test_a_denied_google_account_never_reaches_the_token_endpoint() -> None:
    denied = GoogleIdentity(subject="s", email="stranger@elsewhere.test", email_verified=True)
    app = create_http_app(http_config(), resolver=StubResolver(denied))
    async with build_client_for(app) as client:
        registration = await _register(client)
        authorize = await _authorize(client, registration["client_id"])
        google_state = parse_qs(urlsplit(authorize.headers["location"]).query)["state"][0]
        callback = await client.get(
            "/auth/google/callback", params={"code": "google-code", "state": google_state}
        )

    returned = parse_qs(urlsplit(callback.headers["location"]).query)
    assert returned["error"] == ["access_denied"]
    assert "code" not in returned


async def test_a_request_dialled_at_the_public_hostname_is_not_misdirected() -> None:
    """The server binds loopback but is reached at its public name through a proxy."""
    app = create_http_app(http_config(), resolver=StubResolver(ALLOWED))
    async with build_client_for(app) as client:
        response = await client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0"})

    # Rejected for want of a token, not for the Host header.
    assert response.status_code == 401


async def test_a_request_dialled_at_a_foreign_hostname_is_misdirected() -> None:
    """DNS-rebinding protection refuses a Host this server does not answer to.

    A valid token is needed to reach the check at all: the bearer middleware
    runs first, so an anonymous request is refused as 401 before the Host is
    ever compared.
    """
    config = http_config(stateless=True)
    app = create_http_app(config, resolver=StubResolver(ALLOWED))
    async with app.router.lifespan_context(app):
        async with build_client_for(app) as client:
            registration = await _register(client)
            code = await _sign_in(client, registration["client_id"])
            access_token = await _redeem(client, registration, code)

        auth = {**MCP_HEADERS, "authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://evil.test"
        ) as client:
            response = await client.post(
                "/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )

    assert response.status_code == 421


# -- A hosted server that restarts and scales ------------------------------


async def test_a_token_from_one_instance_opens_the_tools_on_another_sharing_its_store() -> None:
    """What the Firestore store buys a hosted server: sign-ins outlive the instance."""
    store = MemoryTokenStore(now=time.time)
    config = http_config(stateless=True)

    first = create_http_app(config, resolver=StubResolver(ALLOWED), store=store)
    async with build_client_for(first) as client:
        registration = await _register(client)
        code = await _sign_in(client, registration["client_id"])
        access_token = await _redeem(client, registration, code)

    second = create_http_app(config, resolver=StubResolver(ALLOWED), store=store)
    async with second.router.lifespan_context(second), build_client_for(second) as client:
        # Stateless: straight to a tool listing, with no initialize and no session.
        listed = await client.post(
            "/mcp",
            headers={**MCP_HEADERS, "authorization": f"Bearer {access_token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert listed.status_code == 200, listed.text
    assert "mcp-session-id" not in listed.headers
    tools = _sse_payload(listed.text)["result"]["tools"]
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS


def test_a_firestore_store_without_credentials_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/credentials.json")

    with pytest.raises(ConfigError, match="no Google credentials"):
        create_http_app(http_config(oauth_store="firestore"), resolver=StubResolver(ALLOWED))
