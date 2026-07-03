import logging
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIO_DIR = Path("/tmp/stackchan_audio")
AUDIO_DIR.mkdir(exist_ok=True)
TEMP_AUDIO_DIR = AUDIO_DIR / ".tmp"
TEMP_AUDIO_DIR.mkdir(exist_ok=True)

_http_server = None
_http_thread = None


class QuietHandler(SimpleHTTPRequestHandler):
    # Abandoned sockets (firmware reboot / WiFi drop mid-download) self-terminate
    # instead of blocking a handler thread forever.
    timeout = 20

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(AUDIO_DIR), **kwargs)

    def log_message(self, format, *args):
        pass


def start_audio_server(port: int) -> None:
    global _http_server, _http_thread
    if _http_server is not None:
        return
    try:
        # ThreadingHTTPServer: one hung client connection must not starve the
        # rest — the single-threaded HTTPServer deadlocked playback whenever a
        # download was interrupted (root cause of the "one bullet" silence bug).
        _http_server = ThreadingHTTPServer(("0.0.0.0", port), QuietHandler)
        _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
        _http_thread.start()
    except OSError as exc:
        logger.warning("Audio server not started on port %d: %s", port, exc)


def audio_url(host: str, port: int, filename: str) -> str:
    return f"http://{host}:{port}/{filename}"
