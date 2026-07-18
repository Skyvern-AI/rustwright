"""Real-stdio initialize identity test."""

import asyncio
from importlib import metadata
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_smoke import _server_command


def test_stdio_initialize_reports_package_identity():
    async def checks():
        command = _server_command()
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "rustwright-mcp"
                assert initialized.serverInfo.version == metadata.version(
                    "rustwright-mcp"
                )

    asyncio.run(checks())
