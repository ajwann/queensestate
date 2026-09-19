"""Runtime configuration for the hosted HTTP transport.

The stdio transport needs nothing from this module beyond
:func:`load_transport`: its tools reach public ArcGIS endpoints that take no
credentials, so there is no configuration to satisfy. The HTTP transport
additionally needs :class:`HttpConfig`, which is loaded separately so a stdio
server never has to supply the OAuth settings.

Duration environment variables are named ``*_MS`` and given in milliseconds.
They are stored as seconds, the unit the rest of the code works in.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8000
_DEFAULT_MCP_PATH = "/mcp"

#: Where Google returns the user after they approve the sign-in.
GOOGLE_CALLBACK_PATH = "/auth/google/callback"

#: Hosts an OAuth issuer may use without TLS, matching what the MCP SDK allows.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Google's published OpenID Connect endpoints, overridable so tests can point
#: the provider at a local stand-in.
_GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

#: Google signs ID tokens under both spellings of its issuer claim.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

#: OAuth scopes requested from Google. "email" is what the allow list checks;
#: the server asks for nothing else, since it only needs to know who is calling.
GOOGLE_SCOPES = ("openid", "email")

#: The one scope this server issues. Every tool is read-only, so there is
#: nothing finer to divide.
QUEENSESTATE_SCOPE = "queensestate:read"

_DEFAULT_ACCESS_TOKEN_TTL_MS = 60 * 60 * 1000
_DEFAULT_REFRESH_TOKEN_TTL_MS = 30 * 24 * 60 * 60 * 1000

Transport = Literal["stdio", "http"]
TRANSPORTS: tuple[Transport, ...] = ("stdio", "http")

#: Where the HTTP transport keeps OAuth state; see :mod:`queensestate.token_store`.
TokenStoreKind = Literal["memory", "firestore"]
TOKEN_STORES: tuple[TokenStoreKind, ...] = ("memory", "firestore")

_DEFAULT_FIRESTORE_DATABASE = "(default)"


class ConfigError(Exception):
    """Raised when an environment override cannot be used."""


def _read_url(env: Mapping[str, str], key: str, fallback: str) -> str:
    raw = env.get(key)
    if not raw:
        return fallback
    parts = urlsplit(raw)
    if not parts.scheme:
        raise ConfigError(f"{key} is not a valid URL")
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"{key} must use http or https, got {parts.scheme}:")
    if not parts.netloc:
        raise ConfigError(f"{key} is not a valid URL")
    return urlunsplit(parts)


def _read_positive_int(env: Mapping[str, str], key: str, fallback: int) -> int:
    raw = env.get(key)
    if not raw:
        return fallback
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise ConfigError(f"{key} must be a positive integer, got {raw!r}") from error
    if value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class GoogleOAuthConfig:
    """Settings for delegating end-user login to Google.

    ``allowed_emails`` and ``allowed_domains`` are the authorization policy:
    Google proves *who* the caller is, and these decide whether that person may
    use the server. Both are lowercase; email comparison is exact and domain
    comparison is on the part after the ``@``.
    """

    client_id: str
    client_secret: str
    allowed_emails: frozenset[str]
    allowed_domains: frozenset[str]
    #: Set only by an explicit opt-in, and then any Google account is admitted.
    allow_any_account: bool
    authorization_url: str
    token_url: str
    jwks_url: str
    access_token_ttl_seconds: float
    refresh_token_ttl_seconds: float

    def permits(self, email: str, *, email_verified: bool) -> bool:
        """Whether the signed-in Google account may use this server.

        An unverified address is never admitted: on a Workspace-hosted domain
        an unverified address does not prove control of the mailbox, so it
        cannot be matched against a domain allow list.
        """
        if not email_verified:
            return False
        address = email.strip().lower()
        if "@" not in address:
            return False
        if self.allow_any_account:
            return True
        if address in self.allowed_emails:
            return True
        return address.rpartition("@")[2] in self.allowed_domains


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """Settings for the HTTP transport, including its OAuth configuration."""

    #: Interface uvicorn binds. Loopback by default, which suits a server behind
    #: a proxy or tunnel; a server terminating its own TLS binds a real address.
    host: str
    port: int
    #: The externally reachable origin, e.g. ``https://queensestate.example.com``.
    #: It is this server's OAuth issuer identifier, so it must match what
    #: clients actually dial, not the bind address.
    public_url: str
    mcp_path: str
    google: GoogleOAuthConfig
    #: PEM certificate chain and private key. Set together to serve HTTPS
    #: directly, with no proxy in front; left unset the server speaks plain
    #: HTTP and something else is expected to terminate TLS.
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    #: ``memory`` keeps OAuth state in this process, so a restart signs
    #: everyone out; ``firestore`` shares it across restarts and instances.
    oauth_store: TokenStoreKind = "memory"
    firestore_database: str = _DEFAULT_FIRESTORE_DATABASE
    #: Serve Streamable HTTP without sessions, so any instance can answer any
    #: request and a restart strands no connected client.
    stateless: bool = False

    @property
    def serves_tls(self) -> bool:
        """Whether this server terminates TLS itself."""
        return self.tls_certfile is not None and self.tls_keyfile is not None

    @property
    def resource_url(self) -> str:
        """RFC 8707 resource identifier: the MCP endpoint clients get tokens for."""
        return f"{self.public_url}{self.mcp_path}"

    @property
    def callback_url(self) -> str:
        """Redirect URI registered with Google for this deployment."""
        return f"{self.public_url}{GOOGLE_CALLBACK_PATH}"


def _read_readable_file(env: Mapping[str, str], key: str, override: str | None) -> str | None:
    """Resolve a path that must exist and be readable by this process.

    Checked at startup rather than at first connection, so a bad path or a
    key the service user cannot read fails loudly at boot instead of breaking
    the first TLS handshake.
    """
    raw = override if override is not None else (env.get(key) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ConfigError(f"{key} is not a file: {path}")
    try:
        with path.open("rb"):
            pass
    except OSError as error:
        raise ConfigError(f"{key} cannot be read: {path} ({error.strerror})") from error
    return str(path)


def _read_required(env: Mapping[str, str], key: str, hint: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"{key} is required for the http transport. {hint}")
    return value


def _read_flag(env: Mapping[str, str], key: str) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def _read_list(env: Mapping[str, str], key: str) -> frozenset[str]:
    """Parse a comma- or space-separated list, lowercased and de-duplicated."""
    raw = env.get(key) or ""
    return frozenset(item.strip().lower() for item in raw.replace(",", " ").split() if item.strip())


def _read_port(env: Mapping[str, str], key: str, fallback: int) -> int:
    port = _read_positive_int(env, key, fallback)
    if port > 65535:
        raise ConfigError(f"{key} must be a port number between 1 and 65535, got {port}")
    return port


def _read_origin(env: Mapping[str, str], key: str, fallback: str) -> str:
    """Read a base URL and strip any path, query, or fragment.

    RFC 8414 compares issuer identifiers by exact string match, so a stray
    trailing slash here would break client discovery.
    """
    parts = urlsplit(_read_url(env, key, fallback))
    if parts.username or parts.password:
        raise ConfigError(f"{key} must not contain credentials")
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def load_http_config(
    env: Mapping[str, str] | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    public_url: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> HttpConfig:
    """Build the HTTP transport's config from the environment.

    Command-line overrides take precedence over the environment. Google client
    credentials are mandatory and access is denied by default: one of
    ``QUEENSESTATE_ALLOWED_EMAILS``, ``QUEENSESTATE_ALLOWED_DOMAINS``, or an
    explicit ``QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT`` must say who is allowed
    in, so a misconfigured deployment is unreachable rather than open to every
    Google account on the internet.

    Raises:
        ConfigError: if a required setting is missing or malformed.
    """
    env = os.environ if env is None else env

    resolved_host = host or (env.get("QUEENSESTATE_HTTP_HOST") or "").strip() or _DEFAULT_HTTP_HOST
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"port must be between 1 and 65535, got {port}")
    resolved_port = (
        port if port is not None else _read_port(env, "QUEENSESTATE_HTTP_PORT", _DEFAULT_HTTP_PORT)
    )

    origin_env = {"QUEENSESTATE_PUBLIC_URL": public_url} if public_url is not None else env
    origin = _read_origin(
        origin_env, "QUEENSESTATE_PUBLIC_URL", f"http://localhost:{resolved_port}"
    )
    # RFC 8414 requires an HTTPS issuer; the SDK relaxes that for loopback so a
    # server can be tried locally. Checked here so it reads as a configuration
    # error rather than surfacing from deep inside the transport at startup.
    parsed_origin = urlsplit(origin)
    if parsed_origin.scheme != "https" and parsed_origin.hostname not in _LOOPBACK_HOSTS:
        raise ConfigError(
            f"QUEENSESTATE_PUBLIC_URL must be https (or a loopback address), got {origin}. "
            "Terminate TLS in front of this server and set its public https URL here."
        )

    allowed_emails = _read_list(env, "QUEENSESTATE_ALLOWED_EMAILS")
    allowed_domains = _read_list(env, "QUEENSESTATE_ALLOWED_DOMAINS")
    allow_any_account = _read_flag(env, "QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT")
    if not (allowed_emails or allowed_domains or allow_any_account):
        raise ConfigError(
            "No Google accounts are allowed to reach this server. Set "
            "QUEENSESTATE_ALLOWED_EMAILS and/or QUEENSESTATE_ALLOWED_DOMAINS, or set "
            "QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT=true to intentionally admit every "
            "Google account."
        )
    for domain in allowed_domains:
        if "@" in domain or "." not in domain:
            raise ConfigError(
                "QUEENSESTATE_ALLOWED_DOMAINS entries must be bare domains like example.com, "
                f"got {domain!r}"
            )
    for address in allowed_emails:
        if "@" not in address:
            raise ConfigError(
                f"QUEENSESTATE_ALLOWED_EMAILS entries must be addresses, got {address!r}"
            )

    google = GoogleOAuthConfig(
        client_id=_read_required(
            env,
            "QUEENSESTATE_GOOGLE_CLIENT_ID",
            "Create an OAuth 2.0 Web application client at "
            "https://console.cloud.google.com/apis/credentials.",
        ),
        client_secret=_read_required(
            env,
            "QUEENSESTATE_GOOGLE_CLIENT_SECRET",
            "It is shown when the OAuth client is created.",
        ),
        allowed_emails=allowed_emails,
        allowed_domains=allowed_domains,
        allow_any_account=allow_any_account,
        authorization_url=_read_url(
            env, "QUEENSESTATE_GOOGLE_AUTHORIZATION_URL", _GOOGLE_AUTHORIZATION_URL
        ),
        token_url=_read_url(env, "QUEENSESTATE_GOOGLE_TOKEN_URL", _GOOGLE_TOKEN_URL),
        jwks_url=_read_url(env, "QUEENSESTATE_GOOGLE_JWKS_URL", _GOOGLE_JWKS_URL),
        access_token_ttl_seconds=_read_positive_int(
            env, "QUEENSESTATE_ACCESS_TOKEN_TTL_MS", _DEFAULT_ACCESS_TOKEN_TTL_MS
        )
        / 1000,
        refresh_token_ttl_seconds=_read_positive_int(
            env, "QUEENSESTATE_REFRESH_TOKEN_TTL_MS", _DEFAULT_REFRESH_TOKEN_TTL_MS
        )
        / 1000,
    )

    certfile = _read_readable_file(env, "QUEENSESTATE_TLS_CERT", tls_cert)
    keyfile = _read_readable_file(env, "QUEENSESTATE_TLS_KEY", tls_key)
    if (certfile is None) != (keyfile is None):
        missing = "QUEENSESTATE_TLS_KEY" if keyfile is None else "QUEENSESTATE_TLS_CERT"
        raise ConfigError(
            f"{missing} must be set too: serving TLS needs both a certificate and a key"
        )

    store_kind = (env.get("QUEENSESTATE_TOKEN_STORE") or "").strip().lower() or "memory"
    if store_kind not in TOKEN_STORES:
        raise ConfigError(
            f"QUEENSESTATE_TOKEN_STORE must be one of {', '.join(TOKEN_STORES)}, got {store_kind!r}"
        )

    return HttpConfig(
        host=resolved_host,
        port=resolved_port,
        public_url=origin,
        mcp_path=_DEFAULT_MCP_PATH,
        google=google,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
        oauth_store=store_kind,
        firestore_database=(env.get("QUEENSESTATE_FIRESTORE_DATABASE") or "").strip()
        or _DEFAULT_FIRESTORE_DATABASE,
        stateless=_read_flag(env, "QUEENSESTATE_STATELESS_HTTP"),
    )


def load_transport(
    env: Mapping[str, str] | None = None, *, override: str | None = None
) -> Transport:
    """Resolve the transport from a CLI override or ``QUEENSESTATE_TRANSPORT``.

    Raises:
        ConfigError: if the named transport is not one this server implements.
    """
    env = os.environ if env is None else env
    raw = (override or env.get("QUEENSESTATE_TRANSPORT") or "stdio").strip().lower()
    if raw not in TRANSPORTS:
        raise ConfigError(f"unknown transport {raw!r}; expected one of {', '.join(TRANSPORTS)}")
    return raw
