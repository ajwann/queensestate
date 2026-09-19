"""HTTP transport: Streamable HTTP with Google-backed OAuth in front of it.

:mod:`queensestate.main` selects between this and stdio. The tool definitions
are shared - only the transport and its authentication differ.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import GOOGLE_CALLBACK_PATH, QUEENSESTATE_SCOPE, ConfigError, HttpConfig
from .oauth import GoogleAuthError, GoogleAuthorizationServerProvider, GoogleIdentityResolver
from .server import AppState, create_server
from .token_store import TokenStore

_logger = logging.getLogger(__name__)


def _configured_token_store(config: HttpConfig) -> TokenStore | None:
    """The store ``config`` asks for, or ``None`` for the provider's in-memory default.

    Raises:
        ConfigError: if Firestore is asked for but its library or credentials
            are missing, so the server refuses to start rather than failing at
            the first sign-in.
    """
    if config.oauth_store != "firestore":
        return None
    # Imported only here, so the stdio transport and in-memory deployments never
    # need google-cloud-firestore (the ``gcp`` extra).
    try:
        from google.auth.exceptions import DefaultCredentialsError

        from .token_store_firestore import connect
    except ImportError as error:
        raise ConfigError(
            "QUEENSESTATE_TOKEN_STORE=firestore needs the gcp extra: "
            "pip install 'queensestate[gcp]'"
        ) from error
    try:
        store = connect(database=config.firestore_database, now=time.time)
    except DefaultCredentialsError as error:
        raise ConfigError(
            f"QUEENSESTATE_TOKEN_STORE=firestore found no Google credentials: {error}"
        ) from error
    _logger.info("keeping OAuth state in Firestore database %s", config.firestore_database)
    return store


def _auth_settings(config: HttpConfig) -> AuthSettings:
    """Advertise this server as both the authorization server and the resource."""
    return AuthSettings(
        issuer_url=config.public_url,
        resource_server_url=config.resource_url,
        # Tokens are minted here and always stamped with this resource, so the
        # bearer middleware can reject anything issued for somewhere else.
        validate_token_resource=True,
        required_scopes=[QUEENSESTATE_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[QUEENSESTATE_SCOPE], default_scopes=[QUEENSESTATE_SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )


def _transport_security(config: HttpConfig) -> TransportSecuritySettings:
    """Allow the Host and Origin values clients legitimately send.

    DNS-rebinding protection compares the Host header, and the SDK's default
    allows only loopback. A hosted server binds one address but is dialled at
    its public name through a proxy, so that name has to be allowed explicitly
    or every request comes back 421. Loopback stays allowed for local testing.
    """
    public = urlsplit(config.public_url)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public.netloc:
        hosts.append(public.netloc)
        # A default-port URL is dialled with the port omitted or present.
        hosts.append(f"{public.hostname}:*" if public.hostname else public.netloc)
        origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def _error_page(message: str, status_code: int) -> HTMLResponse:
    """A plain page for failures that have no client to redirect back to."""
    body = (
        "<!doctype html><meta charset=utf-8><title>Sign-in failed</title>"
        "<body style='font:16px system-ui;margin:4rem auto;max-width:32rem'>"
        f"<h1>Sign-in failed</h1><p>{message}</p>"
        "<p>Close this window and start the connection again.</p>"
    )
    return HTMLResponse(body, status_code=status_code)


def create_http_server(
    config: HttpConfig,
    *,
    resolver: GoogleIdentityResolver | None = None,
    store: TokenStore | None = None,
) -> MCPServer[AppState]:
    """Build the MCP server with OAuth and the Google callback route attached.

    ``store`` overrides the one ``config`` names, for tests.
    """
    provider = GoogleAuthorizationServerProvider(
        config.google,
        callback_url=config.callback_url,
        resource_url=config.resource_url,
        resolver=resolver,
        store=store if store is not None else _configured_token_store(config),
    )
    server = create_server(auth=_auth_settings(config), auth_server_provider=provider)

    @server.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])  # type: ignore[untyped-decorator]
    async def google_callback(request: Request) -> Response:
        """Where Google returns the user; hands them back to the MCP client."""
        error = request.query_params.get("error")
        if error is not None:
            # The user declined at Google's consent screen, or Google refused.
            return _error_page("Google did not authorize this sign-in.", 400)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return _error_page("This sign-in link is incomplete.", 400)

        try:
            redirect_to = await provider.complete_google_callback(code=code, state=state)
        except GoogleAuthError as failure:
            return _error_page(str(failure), 400)
        return RedirectResponse(redirect_to, status_code=302)

    return server


def create_http_app(
    config: HttpConfig,
    *,
    resolver: GoogleIdentityResolver | None = None,
    store: TokenStore | None = None,
) -> Starlette:
    """Build the ASGI app, for an external server or for tests."""
    server = create_http_server(config, resolver=resolver, store=store)
    return server.streamable_http_app(
        streamable_http_path=config.mcp_path,
        stateless_http=config.stateless,
        host=config.host,
        transport_security=_transport_security(config),
    )


async def serve_http(config: HttpConfig) -> None:
    """Run the HTTP transport until the process is stopped.

    uvicorn is driven directly rather than through the SDK's helper, because
    only this way can the server terminate its own TLS - the deployment that
    has no proxy or tunnel in front of it.
    """
    scheme = "https" if config.serves_tls else "http"
    _logger.info("starting on %s://%s:%d%s", scheme, config.host, config.port, config.mcp_path)
    _logger.info("issuer and resource advertised as %s", config.resource_url)
    _logger.info("google redirect URI must be registered as %s", config.callback_url)
    if not config.serves_tls:
        _logger.info("serving plain HTTP; a proxy or tunnel must terminate TLS")
    if config.stateless:
        _logger.info("serving stateless streamable HTTP: no MCP sessions are kept")

    settings = uvicorn.Config(
        create_http_app(config),
        host=config.host,
        port=config.port,
        log_level="warning",
        # Diagnostics go to the same stderr logger as the rest of the server.
        log_config=None,
        ssl_certfile=config.tls_certfile,
        ssl_keyfile=config.tls_keyfile,
    )
    await uvicorn.Server(settings).serve()
