#!/bin/bash
# Start Stack-chan MCP server in HTTP mode + cloudflared tunnel
# For Chat/Cowork access via https://stackchan.migratorybird.xyz
#
# Usage: ./start-http.sh        (start both)
#        ./start-http.sh stop   (stop both)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi

STACKCHAN_PORT="${STACKCHAN_PORT:-8002}"
MCP_PYTHON="${MCP_PYTHON:-}"
MCP_MODULE="${MCP_MODULE:-mcp_server.server}"
STACKCHAN_PUBLIC_MCP_URL="${STACKCHAN_PUBLIC_MCP_URL:-https://stackchan.migratorybird.xyz/mcp}"
STACKCHAN_LOG_DIR="${STACKCHAN_LOG_DIR:-/tmp}"
MCP_LOG="$STACKCHAN_LOG_DIR/stackchan_mcp_http.log"
CLOUDFLARED_LOG="$STACKCHAN_LOG_DIR/cloudflared.log"

if [ "$1" = "stop" ]; then
    echo "Stopping..."
    kill $(lsof -ti:"$STACKCHAN_PORT") 2>/dev/null
    pkill -f "cloudflared tunnel run" 2>/dev/null
    echo "Done."
    exit 0
fi

# Start MCP HTTP server
if lsof -ti:"$STACKCHAN_PORT" >/dev/null 2>&1; then
    echo "⚠️  Port $STACKCHAN_PORT already in use, killing..."
    kill $(lsof -ti:"$STACKCHAN_PORT") 2>/dev/null
    sleep 1
fi

echo "🐋 Starting Stack-chan MCP HTTP server on port $STACKCHAN_PORT..."
if [ -n "$MCP_PYTHON" ]; then
    cd "$SCRIPT_DIR" || exit 1
    nohup "$MCP_PYTHON" -m "$MCP_MODULE" --http --port "$STACKCHAN_PORT" > "$MCP_LOG" 2>&1 &
else
    cd "$SCRIPT_DIR" || exit 1
    nohup uv run python -m "$MCP_MODULE" --http --port "$STACKCHAN_PORT" > "$MCP_LOG" 2>&1 &
fi
echo "   PID=$!"

sleep 2

# Start cloudflared (if not already running)
if pgrep -f "cloudflared tunnel run" >/dev/null 2>&1; then
    echo "☁️  cloudflared already running"
else
    echo "☁️  Starting cloudflared tunnel..."
    nohup cloudflared tunnel run > "$CLOUDFLARED_LOG" 2>&1 &
    echo "   PID=$!"
fi

sleep 3

# Verify
echo ""
echo "=== Status ==="
if curl -s --max-time 5 "$STACKCHAN_PUBLIC_MCP_URL" -X POST \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' 2>&1 | grep -q "serverInfo"; then
    echo "✅ $STACKCHAN_PUBLIC_MCP_URL → Streamable HTTP OK"
else
    echo "❌ Tunnel not responding yet (may need a few more seconds)"
fi
echo ""
echo "Claude.ai MCP config:"
echo "  URL: $STACKCHAN_PUBLIC_MCP_URL"
echo "Logs:"
echo "  MCP: $MCP_LOG"
echo "  cloudflared: $CLOUDFLARED_LOG"
