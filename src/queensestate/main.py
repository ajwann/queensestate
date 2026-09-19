"""Entry point: selects the transport and runs the server.

``stdio`` (the default) is for a server the client launches itself (Claude
Code, Claude Desktop); ``http`` is for a hosted server, and authenticates its
callers with Google OAuth.

On stdio, stdout carries protocol traffic only; all diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .config import TRANSPORTS, ConfigError, Transport, load_http_config, load_transport
from .server import create_server

_logger = logging.getLogger("queensestate")


def configure_logging() -> None:
    """Send this server's diagnostics to stderr, keeping stdout free for MCP traffic.

    The root logger stays at WARNING so third-party per-request chatter (httpx
    logs every ArcGIS fetch at INFO) does not reach the launching client's log.
    """
    logging.basicConfig(
        level=logging.WARNING, format="[queensestate] %(message)s", stream=sys.stderr, force=True
    )
    _logger.setLevel(logging.INFO)
    logging.getLogger("queensestate.oauth").setLevel(logging.INFO)
    logging.getLogger("queensestate.http").setLevel(logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Every option also has an environment variable, for hosted runs."""
    parser = argparse.ArgumentParser(
        prog="queensestate",
        description="MCP server for Charlotte open city data.",
    )
    parser.add_argument("--version", action="version", version=f"queensestate {__version__}")
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=None,
        help="Transport to serve on (env QUEENSESTATE_TRANSPORT; default stdio).",
    )
    http_options = parser.add_argument_group("http transport")
    http_options.add_argument(
        "--host",
        default=None,
        help="Interface to bind (env QUEENSESTATE_HTTP_HOST; default 127.0.0.1).",
    )
    http_options.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind (env QUEENSESTATE_HTTP_PORT; default 8000).",
    )
    http_options.add_argument(
        "--tls-cert",
        default=None,
        help=(
            "PEM certificate chain, to serve HTTPS directly with no proxy in "
            "front (env QUEENSESTATE_TLS_CERT). Requires --tls-key."
        ),
    )
    http_options.add_argument(
        "--tls-key",
        default=None,
        help="PEM private key (env QUEENSESTATE_TLS_KEY). Requires --tls-cert.",
    )
    http_options.add_argument(
        "--public-url",
        default=None,
        help=(
            "Externally reachable origin, e.g. https://queensestate.example.com "
            "(env QUEENSESTATE_PUBLIC_URL). It is this server's OAuth issuer, so it "
            "must match the URL clients dial."
        ),
    )
    return parser


async def serve(args: argparse.Namespace, transport: Transport) -> None:
    """Run the selected transport until the client disconnects or the process stops."""
    if transport == "http":
        # Imported here so a stdio-only run never pays for starlette/uvicorn,
        # and a missing optional dependency cannot break the local path.
        from .http import serve_http

        http_config = load_http_config(
            host=args.host,
            port=args.port,
            public_url=args.public_url,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
        await serve_http(http_config)
        return

    _logger.info("starting on stdio")
    await create_server().run_stdio_async()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    configure_logging()
    # httpx logs every request URL at INFO, which drowns out the server's own logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    try:
        transport = load_transport(override=args.transport)
        asyncio.run(serve(args, transport))
    except ConfigError as error:
        _logger.error("configuration error: %s", error)
        return 1
    except KeyboardInterrupt:
        _logger.info("received interrupt, shutting down")
    except Exception:
        _logger.exception("fatal error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
