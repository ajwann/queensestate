"""The token-store contract, run against every implementation.

The Firestore store runs only against the emulator: start one with
``gcloud emulators firestore start`` and set ``FIRESTORE_EMULATOR_HOST`` to
include it. Each test gets its own collection prefix, so runs cannot collide.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

import pytest
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from pydantic import AnyUrl
from test_oauth import CLIENT_REDIRECT, RESOURCE_URL, Clock, client, params

from queensestate.config import QUEENSESTATE_SCOPE
from queensestate.token_store import MemoryTokenStore, PendingAuthorization, TokenStore

#: Small, so eviction is exercised without registering a thousand clients.
MAX_CLIENTS = 3

EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST")
needs_emulator = pytest.mark.skipif(
    not EMULATOR_HOST, reason="set FIRESTORE_EMULATOR_HOST to test against the Firestore emulator"
)

pytestmark = pytest.mark.anyio


class StoreFactory(Protocol):
    """Builds a store on a given clock, optionally under a fixed collection prefix.

    The prefix only means anything to Firestore, where it keeps concurrent runs
    from colliding; the memory store accepts and ignores it so both
    implementations satisfy one interface.
    """

    def __call__(self, clock: Clock, prefix: str | None = None) -> TokenStore: ...


def _memory(clock: Clock, prefix: str | None = None) -> TokenStore:
    return MemoryTokenStore(now=clock, max_clients=MAX_CLIENTS)


def firestore_client(open_clients: list[Any]) -> Any:
    """An emulator-backed client, registered so the caller can close it.

    A Firestore AsyncClient holds gRPC machinery that outlives the object, so
    each one is handed back through open_clients to be closed once the work is
    done. What keeps that machinery off a dead event loop is the session-wide
    loop in conftest, not this bookkeeping; closing the transport is ordinary
    hygiene.
    """
    from google.cloud.firestore import AsyncClient

    database = AsyncClient(project="queensestate-test")
    open_clients.append(database)
    return database


def _firestore(open_clients: list[Any]) -> StoreFactory:
    def build(clock: Clock, prefix: str | None = None) -> TokenStore:
        from queensestate.token_store_firestore import FirestoreTokenStore

        return FirestoreTokenStore(
            firestore_client(open_clients),
            now=clock,
            prefix=prefix or f"test_{uuid.uuid4().hex}_",
            max_clients=MAX_CLIENTS,
        )

    return build


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param("firestore", id="firestore", marks=needs_emulator),
    ]
)
async def make_store(request: pytest.FixtureRequest) -> AsyncIterator[StoreFactory]:
    open_clients: list[Any] = []
    yield _memory if request.param == "memory" else _firestore(open_clients)
    # AsyncClient.close() is synchronous despite the class name - it closes the
    # underlying transport and returns None, so awaiting it is a TypeError.
    for database in open_clients:
        database.close()


def pending(clock: Clock, ttl: float = 600) -> PendingAuthorization:
    return PendingAuthorization(
        client_id="client-1",
        params=params(),
        google_code_verifier="google-verifier",
        expires_at=clock.now + ttl,
    )


def authorization_code(clock: Clock, value: str = "code-1") -> AuthorizationCode:
    return AuthorizationCode(
        code=value,
        scopes=[QUEENSESTATE_SCOPE],
        expires_at=clock.now + 60,
        client_id="client-1",
        code_challenge="client-code-challenge",
        redirect_uri=AnyUrl(CLIENT_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE_URL,
        subject="google-sub-1",
    )


def token_pair(
    clock: Clock, access: str = "access-1", refresh: str = "refresh-1"
) -> tuple[AccessToken, RefreshToken]:
    common = {"client_id": "client-1", "scopes": [QUEENSESTATE_SCOPE], "resource": RESOURCE_URL}
    return (
        AccessToken(token=access, expires_at=int(clock.now + 3600), subject="s", **common),
        RefreshToken(token=refresh, expires_at=int(clock.now + 86400), subject="s", **common),
    )


# -- Clients -----------------------------------------------------------------


async def test_a_registration_reads_back_unchanged(make_store: StoreFactory) -> None:
    store = make_store(Clock())
    registered = client()

    await store.save_client(registered)

    assert await store.get_client("client-1") == registered
    assert await store.get_client("never-registered") is None


async def test_registrations_past_the_ceiling_evict_the_oldest(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    for index in range(MAX_CLIENTS + 1):
        await store.save_client(client(f"client-{index}"))
        clock.now += 1

    assert await store.get_client("client-0") is None
    for index in range(1, MAX_CLIENTS + 1):
        assert await store.get_client(f"client-{index}") is not None


async def test_a_client_id_cannot_address_another_document(make_store: StoreFactory) -> None:
    store = make_store(Clock())
    await store.save_client(client())

    # Path-shaped ids from a request are just unknown clients, never an error.
    for hostile in ("../client-1", "client-1/sub/doc", "a/b"):
        assert await store.get_client(hostile) is None


# -- Pending authorizations ----------------------------------------------------


async def test_a_pending_authorization_is_taken_exactly_once(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    parked = pending(clock)

    await store.save_pending("state-1", parked)
    assert await store.count_pending() == 1

    assert await store.take_pending("state-1") == parked
    assert await store.take_pending("state-1") is None
    assert await store.count_pending() == 0


async def test_an_expired_authorization_is_neither_counted_nor_returned(
    make_store: StoreFactory,
) -> None:
    clock = Clock()
    store = make_store(clock)
    await store.save_pending("state-1", pending(clock, ttl=10))

    clock.now += 11

    assert await store.count_pending() == 0
    assert await store.take_pending("state-1") is None


# -- Authorization codes -------------------------------------------------------


async def test_a_code_is_readable_until_it_is_taken(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    code = authorization_code(clock)
    await store.save_code(code)

    assert await store.get_code("code-1") == code
    assert await store.take_code("code-1") == code
    assert await store.get_code("code-1") is None
    assert await store.take_code("code-1") is None


async def test_an_expired_code_is_not_returned(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    await store.save_code(authorization_code(clock))

    clock.now += 61

    assert await store.get_code("code-1") is None
    assert await store.take_code("code-1") is None


# -- Tokens ----------------------------------------------------------------------


async def test_tokens_read_back_unchanged(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    access, refresh = token_pair(clock)

    await store.save_tokens(access, refresh)

    assert await store.get_access_token("access-1") == access
    assert await store.get_refresh_token("refresh-1") == refresh
    assert await store.get_access_token("refresh-1") is None


async def test_taking_a_refresh_token_also_drops_its_access_token(
    make_store: StoreFactory,
) -> None:
    clock = Clock()
    store = make_store(clock)
    access, refresh = token_pair(clock)
    await store.save_tokens(access, refresh)

    assert await store.take_refresh_token("refresh-1") == refresh
    assert await store.take_refresh_token("refresh-1") is None
    assert await store.get_access_token("access-1") is None


async def test_revoking_either_token_drops_the_pair(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    await store.save_tokens(*token_pair(clock, "access-1", "refresh-1"))
    await store.save_tokens(*token_pair(clock, "access-2", "refresh-2"))

    await store.revoke_access_token("access-1")
    await store.revoke_refresh_token("refresh-2")

    for token in ("access-1", "access-2"):
        assert await store.get_access_token(token) is None
    for token in ("refresh-1", "refresh-2"):
        assert await store.get_refresh_token(token) is None


async def test_revoking_an_unknown_token_is_harmless(make_store: StoreFactory) -> None:
    store = make_store(Clock())

    await store.revoke_access_token("never-issued")
    await store.revoke_refresh_token("never-issued")


async def test_expired_tokens_are_not_returned(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock)
    await store.save_tokens(*token_pair(clock))

    clock.now += 3601
    assert await store.get_access_token("access-1") is None
    assert await store.get_refresh_token("refresh-1") is not None

    clock.now += 86400
    assert await store.get_refresh_token("refresh-1") is None
    assert await store.take_refresh_token("refresh-1") is None


# -- Firestore at rest -------------------------------------------------------------


@needs_emulator
async def test_firestore_never_stores_a_secret_that_could_be_presented() -> None:
    clock = Clock()
    prefix = f"test_{uuid.uuid4().hex}_"
    open_clients: list[Any] = []
    store = _firestore(open_clients)(clock, prefix)
    access, refresh = token_pair(clock, "access-secret", "refresh-secret")
    await store.save_pending("state-secret", pending(clock))
    await store.save_code(authorization_code(clock, "code-secret"))
    await store.save_tokens(access, refresh)

    stored: list[str] = []
    database = firestore_client(open_clients)
    try:
        for kind in ("pending", "codes", "access_tokens", "refresh_tokens"):
            async for snapshot in database.collection(f"{prefix}{kind}").stream():
                stored.append(f"{snapshot.id} {snapshot.to_dict()}")
    finally:
        for opened in open_clients:
            opened.close()

    assert len(stored) == 4
    dump = "\n".join(stored)
    for secret in ("state-secret", "code-secret", "access-secret", "refresh-secret"):
        assert secret not in dump
