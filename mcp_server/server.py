"""
stackchan-mcp: MCP server for Stack-chan voice control.

Usage:
  python -m mcp_server.server
  python -m mcp_server.server --http --port 8001
"""

import hmac
import json
import os
import sys

from mcp.server.fastmcp import FastMCP, Image
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .audio_server import start_audio_server
from .mcp_tools import register_tools
from .stackchan_client import StackchanClient
from .stackchan_config import StackchanConfig, config_summary, load_config


def parse_args(argv: list[str]) -> tuple[bool, str, int]:
    http_mode = "--http" in argv
    host = os.environ.get("STACKCHAN_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("STACKCHAN_MCP_HTTP_PORT", "8002"))
    for index, arg in enumerate(argv):
        if arg == "--host" and index + 1 < len(argv):
            host = argv[index + 1]
        if arg == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
    return http_mode, host, port


def ensure_http_auth_configured(config: StackchanConfig) -> None:
    """Fail closed: refuse to start the HTTP transport without a bearer token.

    The MCP HTTP server drives real device actions (say/see/camera). It must
    never be reachable through a public tunnel without authentication.
    """
    if not config.mcp_auth_token:
        raise RuntimeError(
            "STACKCHAN_MCP_AUTH_TOKEN is not set; refusing to start the MCP HTTP "
            "server without authentication"
        )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <token>` on every inbound HTTP request."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential, self._token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_mcp(
    config: StackchanConfig,
    *,
    http_mode: bool = False,
    host: str = "127.0.0.1",
    port: int = 8002,
):
    client = StackchanClient(config)
    mcp = FastMCP("stackchan", host=host, port=port) if http_mode else FastMCP("stackchan")
    register_tools(mcp, client, config, Image)
    return mcp


if __name__ == "__main__":
    config = load_config()
    http_mode, mcp_host, mcp_port = parse_args(sys.argv)
    mcp = create_mcp(config, http_mode=http_mode, host=mcp_host, port=mcp_port)
    start_audio_server(config.audio_serve_port)
    if http_mode:
        try:
            ensure_http_auth_configured(config)
        except RuntimeError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
            sys.exit(1)
        print(
            json.dumps(
                {
                    "ok": True,
                    "service": "stackchan_mcp_server",
                    "http": {"host": mcp_host, "port": mcp_port},
                    "config": config_summary(config),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        import uvicorn

        http_app = mcp.streamable_http_app()
        http_app.add_middleware(BearerAuthMiddleware, token=config.mcp_auth_token)
        uvicorn.run(http_app, host=mcp_host, port=mcp_port)
    else:
        mcp.run(transport="stdio")
