import logging
import math
import os
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.communicate()
        return

    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()


def _run_publish(command: list[str], timeout: float) -> None:
    process = subprocess.Popen(  # noqa: S603 - trusted local config, passed as argv without a shell.
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        raise

    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )


def publish_wav(wav_path: Path, target: str, timeout: float, attempts: int = 2) -> None:
    """Atomically publish one WAV to a remote rsync destination."""
    if not target:
        return

    rsync = shutil.which("rsync")
    if not rsync:
        raise RuntimeError("rsync is required when STACKCHAN_AUDIO_PUBLISH_TARGET is set")

    timeout = max(1.0, timeout)
    destination = target if target.endswith("/") else f"{target}/"
    ssh_command = (
        "/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=6 "
        "-o ConnectionAttempts=1 -o ServerAliveInterval=3 -o ServerAliveCountMax=2"
    )
    command = [
        rsync,
        "-a",
        f"--timeout={math.ceil(timeout)}",
        "-e",
        ssh_command,
        str(wav_path),
        destination,
    ]
    attempts = max(1, attempts)
    last_error: subprocess.SubprocessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            _run_publish(command, timeout)
            return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            last_error = exc
            if attempt < attempts:
                logger.warning(
                    "Audio publish attempt %d/%d failed; retrying: %s",
                    attempt,
                    attempts,
                    exc,
                )
                time.sleep(0.5)

    if isinstance(last_error, subprocess.TimeoutExpired):
        raise RuntimeError(
            f"audio publish timed out after {attempts} attempts ({timeout:g}s each)"
        ) from last_error
    if isinstance(last_error, subprocess.CalledProcessError):
        detail = (last_error.stderr or last_error.stdout or "rsync failed").strip().splitlines()[-1]
        raise RuntimeError(
            f"audio publish failed after {attempts} attempts: {detail}"
        ) from last_error
    raise RuntimeError("audio publish failed without a subprocess result")
