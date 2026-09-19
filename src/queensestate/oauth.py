"""OAuth 2.1 authorization server for the HTTP transport, backed by Google.

MCP clients register themselves dynamically and expect an authorization server
at the MCP server's own origin; Google supports neither dynamic registration
nor audience-restricting tokens to a third-party resource. So this server is
its own authorization server and delegates only the *login* to Google:

    MCP client  <--OAuth-->  queensestate (this AS)  <--OAuth-->  Google

The client's tokens are minted here, never passed through from Google. Google's
answer is used once, to learn which account signed in, and the resulting email
is checked against the configured allow list before any code is issued.

What has been issued is kept in a :class:`~queensestate.token_store.TokenStore`:
in memory by default, which is per process - a restart invalidates outstanding
tokens - or in Firestore, for a server that scales to zero or runs several
instances behind one hostname.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import GOOGLE_ISSUERS, GOOGLE_SCOPES, QUEENSESTATE_SCOPE, GoogleOAuthConfig
from .token_store import MemoryTokenStore, PendingAuthorization, TokenStore

_logger = logging.getLogger(__name__)

#: Bytes of entropy behind every code and token. RFC 6749 §10.10 asks for 128
#: bits; this is 256.
_SECRET_BYTES = 32

#: How long a client has to finish the Google round trip and then redeem the
#: code it comes back with.
_PENDING_TTL_SECONDS = 10 * 60
_AUTHORIZATION_CODE_TTL_SECONDS = 60

#: Ceiling on sign-ins in flight. /authorize is reachable before any
#: authentication, so an unbounded store would be a resource-exhaustion lever
#: for anyone who can reach the server.
_MAX_PENDING = 512

#: Bounds the Google token exchange, which happens while a browser waits.
_GOOGLE_TIMEOUT_SECONDS = 15.0


class GoogleAuthError(Exception):
    """A Google sign-in could not be completed or was not permitted."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """The parts of a verified Google ID token this server acts on."""

    #: Google's stable, per-account identifier (the ``sub`` claim).
    subject: str
    email: str
    email_verified: bool


class GoogleIdentityResolver(Protocol):
    """Turns a Google authorization code into a verified identity.

    Injected so tests can exercise the flow without reaching Google.
    """

    async def resolve(self, *, code: str, redirect_uri: str, code_verifier: str) -> GoogleIdentity:
        """Redeem ``code`` at Google and return the identity it proves.

        Raises:
            GoogleAuthError: if the exchange fails or the ID token is invalid.
        """
        ...


class HttpGoogleIdentityResolver:
    """Talks to Google's real token endpoint and verifies the ID token it returns."""

    def __init__(self, config: GoogleOAuthConfig) -> None:
        self._config = config
        # PyJWKClient caches Google's signing keys across requests, so the JWKS
        # is fetched about once per key rotation rather than once per login.
        self._jwks = jwt.PyJWKClient(config.jwks_url, timeout=_GOOGLE_TIMEOUT_SECONDS)

    async def resolve(self, *, code: str, redirect_uri: str, code_verifier: str) -> GoogleIdentity:
        id_token = await self._exchange(
            code=code, redirect_uri=redirect_uri, verifier=code_verifier
        )
        # PyJWKClient uses blocking I/O for the JWKS fetch; keep it off the loop.
        claims = await asyncio.to_thread(self._verify, id_token)
        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not isinstance(email, str):
            raise GoogleAuthError("Google ID token carried no account identity")
        return GoogleIdentity(
            subject=subject, email=email, email_verified=claims.get("email_verified") is True
        )

    async def _exchange(self, *, code: str, redirect_uri: str, verifier: str) -> str:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code_verifier": verifier,
        }
        try:
            async with httpx.AsyncClient(timeout=_GOOGLE_TIMEOUT_SECONDS) as client:
                response = await client.post(self._config.token_url, data=form)
        except httpx.HTTPError as error:
            raise GoogleAuthError(f"Google token request failed: {error}") from error
        if response.status_code != 200:
            # Google's body echoes the request; log the code only, never the body.
            raise GoogleAuthError(f"Google token endpoint returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise GoogleAuthError("Google token endpoint returned a malformed body") from error
        id_token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(id_token, str):
            raise GoogleAuthError("Google token response carried no id_token")
        return id_token

    def _verify(self, id_token: str) -> dict[str, Any]:
        try:
            key = self._jwks.get_signing_key_from_jwt(id_token)
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self._config.client_id,
                issuer=list(GOOGLE_ISSUERS),
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise GoogleAuthError(f"Google ID token failed verification: {error}") from error
        return claims


def _new_secret() -> str:
    return secrets.token_urlsafe(_SECRET_BYTES)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class GoogleAuthorizationServerProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth authorization server whose user authentication is Google sign-in.

    The SDK's handlers own protocol validation - PKCE, redirect-URI matching,
    code expiry, client authentication - so this class is the Google round trip
    and the policy around it, with state kept in a :class:`TokenStore`. The
    unimplemented ``exchange_identity_assertion`` is inherited: this server has
    no enterprise IdP behind it.
    """

    def __init__(
        self,
        config: GoogleOAuthConfig,
        *,
        callback_url: str,
        resource_url: str,
        resolver: GoogleIdentityResolver | None = None,
        store: TokenStore | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._callback_url = callback_url
        self._resource_url = resource_url
        self._resolver = resolver if resolver is not None else HttpGoogleIdentityResolver(config)
        self._now = now
        self._store: TokenStore = store if store is not None else MemoryTokenStore(now=now)

    # -- Dynamic client registration -------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._store.save_client(client_info)

    # -- Authorization ----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to Google to sign in."""
        if params.resource is not None and not self._is_this_resource(params.resource):
            raise AuthorizeError(
                error="invalid_target",
                error_description=f"This server issues tokens for {self._resource_url} only",
            )
        if await self._store.count_pending() >= _MAX_PENDING:
            raise AuthorizeError(
                error="temporarily_unavailable",
                error_description="Too many sign-ins in progress; try again shortly",
            )

        state = _new_secret()
        verifier = _new_secret()
        await self._store.save_pending(
            state,
            PendingAuthorization(
                client_id=client.client_id,
                params=params,
                google_code_verifier=verifier,
                expires_at=self._now() + _PENDING_TTL_SECONDS,
            ),
        )

        query = urlencode(
            {
                "client_id": self._config.client_id,
                "redirect_uri": self._callback_url,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SCOPES),
                "state": state,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
                # Ask for the account chooser rather than silently reusing a
                # session, so the operator can pick an allowed account.
                "prompt": "select_account",
            }
        )
        return f"{self._config.authorization_url}?{query}"

    async def complete_google_callback(self, *, code: str, state: str) -> str:
        """Finish the Google leg and return where to send the browser next.

        On success that is the MCP client's redirect URI carrying a fresh
        authorization code; on a denied account it is the same URI carrying an
        OAuth error, so the client shows a real failure instead of hanging.

        Raises:
            GoogleAuthError: when the request cannot be tied back to a pending
                authorization, leaving nowhere safe to redirect.
        """
        pending = await self._store.take_pending(state)
        if pending is None:
            raise GoogleAuthError("This sign-in link has expired or was already used")

        redirect_uri = str(pending.params.redirect_uri)
        try:
            identity = await self._resolver.resolve(
                code=code,
                redirect_uri=self._callback_url,
                code_verifier=pending.google_code_verifier,
            )
        except GoogleAuthError as error:
            _logger.warning("google sign-in failed: %s", error)
            return construct_redirect_uri(
                redirect_uri,
                error="access_denied",
                error_description="Google sign-in failed",
                state=pending.params.state,
            )

        if not self._config.permits(identity.email, email_verified=identity.email_verified):
            _logger.warning("denied sign-in for google subject %s", identity.subject)
            return construct_redirect_uri(
                redirect_uri,
                error="access_denied",
                error_description="This Google account is not allowed to use this server",
                state=pending.params.state,
            )

        authorization_code = _new_secret()
        await self._store.save_code(
            AuthorizationCode(
                code=authorization_code,
                scopes=pending.params.scopes or [QUEENSESTATE_SCOPE],
                expires_at=self._now() + _AUTHORIZATION_CODE_TTL_SECONDS,
                client_id=pending.client_id,
                code_challenge=pending.params.code_challenge,
                redirect_uri=pending.params.redirect_uri,
                redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
                # Always this server, never the client-supplied indicator: it is
                # the only resource these tokens are good for.
                resource=self._resource_url,
                subject=identity.subject,
            )
        )
        _logger.info("issued authorization code to client %s", pending.client_id)
        return construct_redirect_uri(
            redirect_uri, code=authorization_code, state=pending.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = await self._store.get_code(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    # -- Tokens -----------------------------------------------------------

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: the code is gone whether or not the rest succeeds.
        if await self._store.take_code(authorization_code.code) is None:
            raise TokenError(
                error="invalid_grant", error_description="Authorization code already redeemed"
            )
        return await self._issue(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = await self._store.get_refresh_token(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token dies with the tokens it minted.
        if await self._store.take_refresh_token(refresh_token.token) is None:
            raise TokenError(
                error="invalid_grant", error_description="Refresh token already used or revoked"
            )
        return await self._issue(
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            subject=refresh_token.subject,
            resource=refresh_token.resource,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await self._store.get_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke the presented token and its counterpart."""
        if isinstance(token, AccessToken):
            await self._store.revoke_access_token(token.token)
        else:
            await self._store.revoke_refresh_token(token.token)

    # -- Internals --------------------------------------------------------

    async def _issue(
        self, *, client_id: str, scopes: list[str], subject: str | None, resource: str | None
    ) -> OAuthToken:
        now = self._now()
        granted = scopes or [QUEENSESTATE_SCOPE]
        access = AccessToken(
            token=_new_secret(),
            client_id=client_id,
            scopes=granted,
            expires_at=int(now + self._config.access_token_ttl_seconds),
            resource=resource or self._resource_url,
            subject=subject,
        )
        refresh = RefreshToken(
            token=_new_secret(),
            client_id=client_id,
            scopes=granted,
            expires_at=int(now + self._config.refresh_token_ttl_seconds),
            resource=resource or self._resource_url,
            subject=subject,
        )
        await self._store.save_tokens(access, refresh)
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",  # noqa: S106 (the RFC 6749 token type, not a secret)
            expires_in=int(self._config.access_token_ttl_seconds),
            scope=" ".join(granted),
            refresh_token=refresh.token,
        )

    def _is_this_resource(self, resource: str) -> bool:
        return resource.rstrip("/") == self._resource_url.rstrip("/")
