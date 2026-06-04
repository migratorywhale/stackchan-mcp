"""
stackchan-mcp: MCP server for Stack-chan voice control.

Usage:
  python -m mcp_server.server
  python -m mcp_server.server --http --port 8001
"""

import sys

from mcp.server.fastmcp import FastMCP, Image

from .audio_server import start_audio_server
from .mcp_tools import register_tools
from .stackchan_client import StackchanClient
from .stackchan_config import StackchanConfig, load_config


def parse_args(argv: list[str]) -> tuple[bool, int]:
    http_mode = "--http" in argv
    port = 8002
    for index, arg in enumerate(argv):
        if arg == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
    return http_mode, port


def create_mcp(config: StackchanConfig, *, http_mode: bool = False, port: int = 8002):
    client = StackchanClient(config)
    mcp = FastMCP("stackchan", host="127.0.0.1", port=port) if http_mode else FastMCP("stackchan")
    register_tools(mcp, client, config, Image)
    return mcp


if __name__ == "__main__":
    config = load_config()
    http_mode, mcp_port = parse_args(sys.argv)
    mcp = create_mcp(config, http_mode=http_mode, port=mcp_port)
    start_audio_server(config.audio_serve_port)
    if http_mode:
        print(f"Stack-chan MCP server starting on HTTP port {mcp_port}")
        print(f"Audio server on port {config.audio_serve_port}")
        print(f"Stack-chan at {config.stackchan_ip}:{config.stackchan_port}")
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
