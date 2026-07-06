#!/bin/bash
# Start Stack-chan MCP server in HTTP mode.
# Public cloudflared tunnel startup is opt-in; set
# STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL=1 only when you intend to expose MCP.
#
# Usage: ./start-http.sh        (start local MCP HTTP server)
#        STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL=1 ./start-http.sh
#        ./start-http.sh stop   (stop local server and cloudflared tunnel)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi

STACKCHAN_MCP_HTTP_HOST="${STACKCHAN_MCP_HTTP_HOST:-127.0.0.1}"
STACKCHAN_MCP_HTTP_PORT="${STACKCHAN_MCP_HTTP_PORT:-8002}"
MCP_PYTHON="${MCP_PYTHON:-}"
MCP_MODULE="${MCP_MODULE:-mcp_server.server}"
STACKCHAN_PUBLIC_MCP_URL="${STACKCHAN_PUBLIC_MCP_URL:-}"
STACKCHAN_MCP_AUTH_TOKEN="${STACKCHAN_MCP_AUTH_TOKEN:-}"
STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL="${STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL:-0}"
STACKCHAN_LOG_DIR="${STACKCHAN_LOG_DIR:-/tmp}"
STACKCHAN_MCP_STOP_GRACE_SEC="${STACKCHAN_MCP_STOP_GRACE_SEC:-1}"
STACKCHAN_MCP_STARTUP_WAIT_SEC="${STACKCHAN_MCP_STARTUP_WAIT_SEC:-2}"
STACKCHAN_TUNNEL_WAIT_SEC="${STACKCHAN_TUNNEL_WAIT_SEC:-3}"
STACKCHAN_MCP_HEALTH_TIMEOUT_SEC="${STACKCHAN_MCP_HEALTH_TIMEOUT_SEC:-5}"
MCP_LOG="$STACKCHAN_LOG_DIR/stackchan_mcp_http.log"
CLOUDFLARED_LOG="$STACKCHAN_LOG_DIR/cloudflared.log"

if [ "$1" = "stop" ]; then
    echo "Stopping..."
    kill $(lsof -ti:"$STACKCHAN_MCP_HTTP_PORT") 2>/dev/null
    pkill -f "cloudflared tunnel run" 2>/dev/null
    echo "Done."
    exit 0
fi

# Start MCP HTTP server
if lsof -ti:"$STACKCHAN_MCP_HTTP_PORT" >/dev/null 2>&1; then
    echo "⚠️  Port $STACKCHAN_MCP_HTTP_PORT already in use, killing..."
    kill $(lsof -ti:"$STACKCHAN_MCP_HTTP_PORT") 2>/dev/null
    sleep "$STACKCHAN_MCP_STOP_GRACE_SEC"
fi

echo "🐋 Starting Stack-chan MCP HTTP server on $STACKCHAN_MCP_HTTP_HOST:$STACKCHAN_MCP_HTTP_PORT..."
if [ -n "$MCP_PYTHON" ]; then
    cd "$SCRIPT_DIR" || exit 1
    nohup "$MCP_PYTHON" -m "$MCP_MODULE" --http --host "$STACKCHAN_MCP_HTTP_HOST" --port "$STACKCHAN_MCP_HTTP_PORT" > "$MCP_LOG" 2>&1 &
else
    cd "$SCRIPT_DIR" || exit 1
    nohup uv run python -m "$MCP_MODULE" --http --host "$STACKCHAN_MCP_HTTP_HOST" --port "$STACKCHAN_MCP_HTTP_PORT" > "$MCP_LOG" 2>&1 &
fi
echo "   PID=$!"

sleep "$STACKCHAN_MCP_STARTUP_WAIT_SEC"

if [ "$STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL" = "1" ]; then
    # Start cloudflared (if not already running)
    if pgrep -f "cloudflared tunnel run" >/dev/null 2>&1; then
        echo "☁️  cloudflared already running"
    else
        echo "☁️  Starting cloudflared tunnel..."
        nohup cloudflared tunnel run > "$CLOUDFLARED_LOG" 2>&1 &
        echo "   PID=$!"
    fi
else
    echo "☁️  Public MCP tunnel disabled; set STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL=1 to start cloudflared."
fi

sleep "$STACKCHAN_TUNNEL_WAIT_SEC"

echo ""
echo "=== Status ==="
if [ -n "$STACKCHAN_PUBLIC_MCP_URL" ]; then
    CURL_AUTH_ARGS=()
    if [ -n "$STACKCHAN_MCP_AUTH_TOKEN" ]; then
        CURL_AUTH_ARGS=(-H "Authorization: Bearer ${STACKCHAN_MCP_AUTH_TOKEN}")
    fi
    if curl -s --max-time "$STACKCHAN_MCP_HEALTH_TIMEOUT_SEC" "$STACKCHAN_PUBLIC_MCP_URL" -X POST \
        -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
        "${CURL_AUTH_ARGS[@]}" \
        -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' 2>&1 | grep -q "serverInfo"; then
        echo "✅ $STACKCHAN_PUBLIC_MCP_URL → Streamable HTTP OK"
    else
        echo "❌ Tunnel not responding yet (may need a few more seconds)"
    fi
else
    echo "ℹ️  Public MCP URL not configured; set STACKCHAN_PUBLIC_MCP_URL to verify a tunnel."
fi
echo ""
echo "Claude.ai MCP config:"
if [ -n "$STACKCHAN_PUBLIC_MCP_URL" ]; then
    echo "  URL: $STACKCHAN_PUBLIC_MCP_URL"
else
    echo "  URL: <set STACKCHAN_PUBLIC_MCP_URL>"
fi
echo "Logs:"
echo "  MCP: $MCP_LOG"
if [ "$STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL" = "1" ]; then
    echo "  cloudflared: $CLOUDFLARED_LOG"
fi
