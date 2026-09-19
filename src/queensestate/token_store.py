"""Where the OAuth authorization server keeps what it has issued.

:class:`~queensestate.oauth.GoogleAuthorizationServerProvider` owns the protocol;
a :class:`TokenStore` only remembers state. There are two:

- :class:`MemoryTokenStore`, the default: dictionaries, per process. A restart
  signs everyone out, and replicas behind one hostname cannot share it.
- ``queensestate.token_store_firestore.FirestoreTokenStore``, for a server that
  scales to zero or runs several instances. It lives in its own module so that
  nothing else ever imports the Firestore client library.

Every ``take_*`` operation is atomic: the entry goes to exactly one caller and
is removed, which is what makes authorization codes single use and refresh
tokens rotate. No operation ever returns an expired entry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull

_logger = logging.getLogger(__name__)

#: Ceiling on client registrations. /register is reachable before any
#: authentication, so an unbounded store would be a resource-exhaustion lever
#: for anyone who can reach the server.
MAX_CLIENTS = 1024


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """One authorization in flight: issued at /authorize, consumed at the Google callback."""

    client_id: str
    params: AuthorizationParams
    #: PKCE verifier for this server's own leg with Google.
    google_code_verifier: str
    expires_at: float


def is_expired(expires_at: float | None, now: float) -> bool:
    """Whether an entry stamped ``expires_at`` is past its lifetime; ``None`` never expires."""
    return expires_at is not None and expires_at <= now


class TokenStore(Protocol):
    """State for the OAuth authorization server.

    Keys are the secrets themselves (a ``state``, code, or token string) except
    for clients, which are keyed by their public ``client_id``.
    """

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """The registration for ``client_id``, or ``None`` if unknown or evicted."""
        ...

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        """Record a registration, evicting the oldest once over the ceiling.

        Eviction rather than refusal: a client whose registration is dropped
        simply registers again, where a hard refusal would wedge the server.
        """
        ...

    async def count_pending(self) -> int:
        """How many unexpired authorizations are in flight."""
        ...

    async def save_pending(self, state: str, pending: PendingAuthorization) -> None:
        """Park an authorization under the ``state`` sent to Google."""
        ...

    async def take_pending(self, state: str) -> PendingAuthorization | None:
        """Remove and return the authorization parked under ``state``."""
        ...

    async def save_code(self, code: AuthorizationCode) -> None:
        """Record an authorization code issued to a client."""
        ...

    async def get_code(self, code: str) -> AuthorizationCode | None:
        """Look up an authorization code without consuming it."""
        ...

    async def take_code(self, code: str) -> AuthorizationCode | None:
        """Remove and return an authorization code: the redemption itself."""
        ...

    async def save_tokens(self, access: AccessToken, refresh: RefreshToken) -> None:
        """Record an access token and the refresh token minted with it, as a pair."""
        ...

    async def get_access_token(self, token: str) -> AccessToken | None:
        """Look up an access token."""
        ...

    async def get_refresh_token(self, token: str) -> RefreshToken | None:
        """Look up a refresh token without consuming it."""
        ...

    async def take_refresh_token(self, token: str) -> RefreshToken | None:
        """Remove and return a refresh token, dropping its paired access token too."""
        ...

    async def revoke_access_token(self, token: str) -> None:
        """Drop an access token and its paired refresh token; unknown tokens are ignored."""
        ...

    async def revoke_refresh_token(self, token: str) -> None:
        """Drop a refresh token and its paired access token; unknown tokens are ignored."""
        ...


class MemoryTokenStore:
    """A :class:`TokenStore` in dictionaries: per process, lost on restart.

    Expired entries are swept on every operation, which keeps the dictionaries
    bounded without a background task.
    """

    def __init__(self, *, now: Callable[[], float], max_clients: int = MAX_CLIENTS) -> None:
        self._now = now
        self._max_clients = max_clients
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        #: access token -> refresh token, so revoking either drops the pair.
        self._paired: dict[str, str] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        self._clients[client.client_id] = client
        while len(self._clients) > self._max_clients:
            evicted = next(iter(self._clients))
            del self._clients[evicted]
            _logger.info("evicted the oldest client registration %s", evicted)

    async def count_pending(self) -> int:
        self._expire()
        return len(self._pending)

    async def save_pending(self, state: str, pending: PendingAuthorization) -> None:
        self._pending[state] = pending

    async def take_pending(self, state: str) -> PendingAuthorization | None:
        self._expire()
        return self._pending.pop(state, None)

    async def save_code(self, code: AuthorizationCode) -> None:
        self._codes[code.code] = code

    async def get_code(self, code: str) -> AuthorizationCode | None:
        self._expire()
        return self._codes.get(code)

    async def take_code(self, code: str) -> AuthorizationCode | None:
        self._expire()
        return self._codes.pop(code, None)

    async def save_tokens(self, access: AccessToken, refresh: RefreshToken) -> None:
        self._access_tokens[access.token] = access
        self._refresh_tokens[refresh.token] = refresh
        self._paired[access.token] = refresh.token

    async def get_access_token(self, token: str) -> AccessToken | None:
        self._expire()
        return self._access_tokens.get(token)

    async def get_refresh_token(self, token: str) -> RefreshToken | None:
        self._expire()
        return self._refresh_tokens.get(token)

    async def take_refresh_token(self, token: str) -> RefreshToken | None:
        self._expire()
        refresh = self._refresh_tokens.pop(token, None)
        if refresh is not None:
            self._drop_paired_access_token(token)
        return refresh

    async def revoke_access_token(self, token: str) -> None:
        self._access_tokens.pop(token, None)
        paired = self._paired.pop(token, None)
        if paired is not None:
            self._refresh_tokens.pop(paired, None)

    async def revoke_refresh_token(self, token: str) -> None:
        self._refresh_tokens.pop(token, None)
        self._drop_paired_access_token(token)

    def _drop_paired_access_token(self, refresh_token: str) -> None:
        for access, paired in list(self._paired.items()):
            if paired == refresh_token:
                del self._paired[access]
                self._access_tokens.pop(access, None)

    def _expire(self) -> None:
        now = self._now()
        for state, pending in list(self._pending.items()):
            if is_expired(pending.expires_at, now):
                del self._pending[state]
        for value, code in list(self._codes.items()):
            if is_expired(code.expires_at, now):
                del self._codes[value]
        for value, access in list(self._access_tokens.items()):
            if is_expired(access.expires_at, now):
                del self._access_tokens[value]
                self._paired.pop(value, None)
        for value, refresh in list(self._refresh_tokens.items()):
            if is_expired(refresh.expires_at, now):
                del self._refresh_tokens[value]
