import math
import shutil
import subprocess
from pathlib import Path


def publish_wav(wav_path: Path, target: str, timeout: float) -> None:
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
    try:
        subprocess.run(  # noqa: S603 - trusted local config, passed as argv without a shell.
            [
                rsync,
                "-a",
                f"--timeout={math.ceil(timeout)}",
                "-e",
                ssh_command,
                str(wav_path),
                destination,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"audio publish timed out after {timeout:g}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "rsync failed").strip().splitlines()[-1]
        raise RuntimeError(f"audio publish failed: {detail}") from exc
