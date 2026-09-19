"""A :class:`~queensestate.token_store.TokenStore` in Cloud Firestore.

For a server that scales to zero or runs more than one instance: sign-ins
survive a cold start and every instance sees the same tokens. Imported only
when ``QUEENSESTATE_TOKEN_STORE=firestore``, so the stdio transport and the in-memory
store never need google-cloud-firestore (the ``gcp`` extra).

One collection per kind, each name carrying a configurable prefix. Every
document id is the SHA-256 of its key:

- ``clients``: the registration, stored whole, client secret included, because
  the SDK authenticates a client by comparing that secret. A registration
  grants nothing by itself - anyone can make one at /register - so it is not a
  credential worth protecting at rest. The id is still hashed, since it arrives
  in requests and must not be able to name a different document path.
- ``pending``, ``codes``, ``access_tokens``, ``refresh_tokens``: the secret is
  left out of the body, so reading the database yields nothing that can be
  presented to this server. It is restored from what the caller presented.

Every document carries ``expires``, a timestamp the deployment attaches a TTL
policy to so that Firestore deletes it eventually. TTL deletion can lag by up
to a day, so every read checks expiry itself as well.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from google.api_core.exceptions import NotFound
from google.cloud.firestore import (
    AsyncClient,
    AsyncCollectionReference,
    AsyncDocumentReference,
    AsyncQuery,
    AsyncTransaction,
    FieldFilter,
    async_transactional,
)
from google.cloud.firestore_v1.async_aggregation import AsyncAggregationQuery
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull

from .token_store import MAX_CLIENTS, PendingAuthorization, is_expired

_logger = logging.getLogger(__name__)

#: How long an unused client registration is kept. Renewed whenever the client
#: is issued tokens, so an active client's registration outlives its tokens.
CLIENT_TTL_SECONDS = 90 * 24 * 60 * 60

#: Collection prefix for a production deployment; tests pass their own so runs
#: against a shared emulator cannot see each other.
DEFAULT_PREFIX = "oauth_"

Body = dict[str, Any]


def _key(value: str) -> str:
    """Document id for ``value``: fixed-length, path-safe, and not the secret itself."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, UTC)


class FirestoreTokenStore:
    """A :class:`~queensestate.token_store.TokenStore` shared through Firestore."""

    def __init__(
        self,
        client: AsyncClient,
        *,
        now: Callable[[], float],
        prefix: str = DEFAULT_PREFIX,
        max_clients: int = MAX_CLIENTS,
    ) -> None:
        self._db = client
        self._now = now
        self._max_clients = max_clients
        self._clients = client.collection(f"{prefix}clients")
        self._pending = client.collection(f"{prefix}pending")
        self._codes = client.collection(f"{prefix}codes")
        self._access_tokens = client.collection(f"{prefix}access_tokens")
        self._refresh_tokens = client.collection(f"{prefix}refresh_tokens")

    # -- Clients ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        body = await self._read(self._clients.document(_key(client_id)))
        if body is None:
            return None
        return OAuthClientInformationFull.model_validate(body["client"])

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        now = self._now()
        await self._clients.document(_key(client.client_id)).set(
            {
                "client": client.model_dump(mode="json"),
                "registered_at": now,
                "expires": _timestamp(now + CLIENT_TTL_SECONDS),
            }
        )
        excess = await self._count(self._clients) - self._max_clients
        if excess <= 0:
            return
        async for snapshot in self._clients.order_by("registered_at").limit(excess).stream():
            await snapshot.reference.delete()
            _logger.info("evicted the oldest client registration %s", snapshot.id)

    # -- Pending authorizations ---------------------------------------------

    async def count_pending(self) -> int:
        unexpired = self._pending.where(filter=FieldFilter("expires", ">", _timestamp(self._now())))
        return await self._count(unexpired)

    async def save_pending(self, state: str, pending: PendingAuthorization) -> None:
        await self._pending.document(_key(state)).set(
            {
                "client_id": pending.client_id,
                "params": pending.params.model_dump(mode="json"),
                "google_code_verifier": pending.google_code_verifier,
                "expires_at": pending.expires_at,
                "expires": _timestamp(pending.expires_at),
            }
        )

    async def take_pending(self, state: str) -> PendingAuthorization | None:
        body = await self._take(self._pending.document(_key(state)))
        if body is None:
            return None
        return PendingAuthorization(
            client_id=str(body["client_id"]),
            params=AuthorizationParams.model_validate(body["params"]),
            google_code_verifier=str(body["google_code_verifier"]),
            expires_at=float(body["expires_at"]),
        )

    # -- Authorization codes --------------------------------------------------

    async def save_code(self, code: AuthorizationCode) -> None:
        await self._codes.document(_key(code.code)).set(
            {
                "code": code.model_dump(mode="json", exclude={"code"}),
                "expires": _timestamp(code.expires_at),
            }
        )

    async def get_code(self, code: str) -> AuthorizationCode | None:
        return self._to_code(code, await self._read(self._codes.document(_key(code))))

    async def take_code(self, code: str) -> AuthorizationCode | None:
        return self._to_code(code, await self._take(self._codes.document(_key(code))))

    # -- Tokens ---------------------------------------------------------------

    async def save_tokens(self, access: AccessToken, refresh: RefreshToken) -> None:
        access_body: Body = {
            "token": access.model_dump(mode="json", exclude={"token"}),
            "refresh": _key(refresh.token),
        }
        refresh_body: Body = {
            "token": refresh.model_dump(mode="json", exclude={"token"}),
            "access": _key(access.token),
        }
        if access.expires_at is not None:
            access_body["expires"] = _timestamp(access.expires_at)
        if refresh.expires_at is not None:
            refresh_body["expires"] = _timestamp(refresh.expires_at)

        batch = self._db.batch()
        batch.set(self._access_tokens.document(_key(access.token)), access_body)
        batch.set(self._refresh_tokens.document(_key(refresh.token)), refresh_body)
        await batch.commit()

        # Keep an active client's registration alive for as long as it is used.
        # NotFound means it was evicted after it was loaded; its next sign-in
        # simply registers it again.
        with contextlib.suppress(NotFound):
            await self._clients.document(_key(access.client_id)).update(
                {"expires": _timestamp(self._now() + CLIENT_TTL_SECONDS)}
            )

    async def get_access_token(self, token: str) -> AccessToken | None:
        body = await self._read(self._access_tokens.document(_key(token)))
        if body is None:
            return None
        return AccessToken.model_validate({**body["token"], "token": token})

    async def get_refresh_token(self, token: str) -> RefreshToken | None:
        body = await self._read(self._refresh_tokens.document(_key(token)))
        if body is None:
            return None
        return RefreshToken.model_validate({**body["token"], "token": token})

    async def take_refresh_token(self, token: str) -> RefreshToken | None:
        body = await self._take(
            self._refresh_tokens.document(_key(token)),
            paired=(self._access_tokens, "access"),
        )
        if body is None:
            return None
        return RefreshToken.model_validate({**body["token"], "token": token})

    async def revoke_access_token(self, token: str) -> None:
        await self._delete_pair(
            self._access_tokens.document(_key(token)),
            paired=(
                self._refresh_tokens,
                "refresh",
            ),
        )

    async def revoke_refresh_token(self, token: str) -> None:
        await self._delete_pair(
            self._refresh_tokens.document(_key(token)),
            paired=(
                self._access_tokens,
                "access",
            ),
        )

    # -- Internals ------------------------------------------------------------

    def _live(self, body: Body) -> bool:
        expires: datetime | None = body.get("expires")
        return expires is None or not is_expired(expires.timestamp(), self._now())

    async def _read(self, ref: AsyncDocumentReference) -> Body | None:
        snapshot = await ref.get()
        body = snapshot.to_dict()
        return body if body is not None and self._live(body) else None

    async def _take(
        self,
        ref: AsyncDocumentReference,
        *,
        paired: tuple[AsyncCollectionReference, str] | None = None,
    ) -> Body | None:
        """Delete ``ref`` and return its body, atomically: one caller wins.

        ``paired`` names the collection and the body field holding the id of a
        counterpart document to delete in the same transaction.
        """

        @async_transactional
        async def take(transaction: AsyncTransaction) -> Body | None:
            snapshot = await ref.get(transaction=transaction)
            body = snapshot.to_dict()
            if body is None:
                return None
            transaction.delete(ref)
            if paired is not None:
                collection, field = paired
                transaction.delete(collection.document(str(body[field])))
            return body

        body = await take(self._db.transaction())
        # An expired entry is consumed all the same; it is simply not honoured.
        return body if body is not None and self._live(body) else None

    async def _delete_pair(
        self, ref: AsyncDocumentReference, *, paired: tuple[AsyncCollectionReference, str]
    ) -> None:
        snapshot = await ref.get()
        body = snapshot.to_dict()
        if body is None:
            return
        collection, field = paired
        batch = self._db.batch()
        batch.delete(ref)
        batch.delete(collection.document(str(body[field])))
        await batch.commit()

    async def _count(self, query: AsyncCollectionReference | AsyncQuery) -> int:
        # count() returns an aggregation instance, but google-cloud-firestore
        # (2.30) annotates it as returning the AsyncAggregationQuery class.
        aggregation = cast("AsyncAggregationQuery", query.count())
        results = await aggregation.get()
        return int(results[0][0].value)

    @staticmethod
    def _to_code(code: str, body: Body | None) -> AuthorizationCode | None:
        if body is None:
            return None
        return AuthorizationCode.model_validate({**body["code"], "code": code})


def connect(
    *, database: str, now: Callable[[], float], project: str | None = None
) -> FirestoreTokenStore:
    """Open a store on ``database`` with Application Default Credentials.

    On Cloud Run the project and credentials come from the metadata server; a
    local run needs ``gcloud auth application-default login`` or the emulator.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: if no credentials are found.
    """
    return FirestoreTokenStore(AsyncClient(project=project, database=database), now=now)
