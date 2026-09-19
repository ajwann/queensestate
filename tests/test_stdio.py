"""The stdio transport, run the way a local MCP client runs it.

These start the real ``python -m queensestate`` in a child process, with every
HTTP, Google, and Firestore setting removed from its environment. Listing tools
reaches no ArcGIS layer, so nothing touches the network. The import-purity test
is what keeps a stdio install free of starlette, uvicorn and the Google
libraries.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from test_http import EXPECTED_TOOLS

pytestmark = pytest.mark.anyio

#: Modules a stdio server must never load: the HTTP transport and everything
#: behind it, including the optional Firestore dependency.
# uvicorn is deliberately absent: mcp.server.mcpserver.server imports it at
# module level, so it arrives with the SDK whatever transport is chosen. What
# this server controls is its own modules and the optional Google libraries.
HTTP_ONLY_MODULES = (
    "queensestate.http",
    "queensestate.oauth",
    "queensestate.token_store",
    "queensestate.token_store_firestore",
    "google.cloud.firestore",
    "google.auth",
)


def _unconfigured_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("QUEENSESTATE_", "GOOGLE_", "FIRESTORE_"))
    }


async def test_stdio_serves_the_tools_with_no_configuration() -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "queensestate"], env=_unconfigured_environment()
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS


def test_the_stdio_path_never_imports_the_http_transport_or_firestore() -> None:
    # A fresh interpreter, because this test process has already imported the
    # HTTP modules for other tests. The probe builds what main.serve builds for
    # stdio, then reports which HTTP-only modules came along.
    probe = "\n".join(
        [
            "import sys",
            "from queensestate.server import create_server",
            "create_server()",
            f"print(','.join(m for m in {HTTP_ONLY_MODULES!r} if m in sys.modules))",
        ]
    )
    result = subprocess.run(  # noqa: S603 (a fixed argument vector, no shell)
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=_unconfigured_environment(),
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == ""
