#!/bin/bash
# Start/stop the host-side Stack-chan voice bridge.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${STACKCHAN_VOICE_BRIDGE_LOG:-/tmp/stackchan_voice_bridge.log}"
PID_FILE="${STACKCHAN_VOICE_BRIDGE_PIDFILE:-/tmp/stackchan_voice_bridge.pid}"
LANGUAGE="${STACKCHAN_VOICE_LANG:-zh}"
INTERVAL="${STACKCHAN_VOICE_INTERVAL:-1}"

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "${1:-start}" in
    start)
        if is_running; then
            echo "Stack-chan voice bridge already running: PID $(cat "$PID_FILE")"
            exit 0
        fi
        cd "$SCRIPT_DIR"
        nohup uv run python scripts/stackchan_voice_bridge.py \
            --lang "$LANGUAGE" \
            --interval "$INTERVAL" \
            >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "Started Stack-chan voice bridge: PID $(cat "$PID_FILE")"
        echo "Log: $LOG_FILE"
        echo "Inbox: ${STACKCHAN_VOICE_INBOX:-/tmp/stackchan_audio/voice_inbox.jsonl}"
        ;;
    stop)
        if is_running; then
            kill "$(cat "$PID_FILE")"
            rm -f "$PID_FILE"
            echo "Stopped Stack-chan voice bridge."
        else
            rm -f "$PID_FILE"
            echo "Stack-chan voice bridge is not running."
        fi
        ;;
    status)
        if is_running; then
            echo "Stack-chan voice bridge running: PID $(cat "$PID_FILE")"
        else
            echo "Stack-chan voice bridge is not running."
        fi
        ;;
    *)
        echo "Usage: $0 [start|stop|status]"
        exit 2
        ;;
esac
