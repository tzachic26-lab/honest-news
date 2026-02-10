import os
import sys

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def run(headline: str) -> None:
    server_path = os.path.join(os.path.dirname(__file__), "servers", "HonestNewsMCPServer.py")
    server = StdioServerParameters(
        command=sys.executable,
        args=[server_path],
        cwd=os.path.dirname(server_path),
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("headline_details", {"headline": headline})
            print(result.structuredContent or result.content)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: uv run python call_headline_details.py \"<headline>\"")
    anyio.run(run, " ".join(sys.argv[1:]).strip())
