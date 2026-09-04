import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .stackchan_config import env_bool, env_float, env_int

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]


def default_tracking_state_path() -> Path:
    configured = os.environ.get("STACKCHAN_FACE_TRACK_STATE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Caches" / "stackchan" / "face-tracking.json"


def acquire_tracker_lock(path: Path | None = None) -> IO[str] | None:
    state_path = path or default_tracking_state_path()
    lock_path = state_path.with_name("face-tracker-process.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


@dataclass(frozen=True)
class TrackingLease:
    sequence: int = 0
    expires_at: float = 0.0
    updated_at: float = 0.0
    reason: str = ""

    def active(self, now: float | None = None) -> bool:
        return self.expires_at > (time.time() if now is None else now)


def _decode_lease(raw: object) -> TrackingLease:
    if not isinstance(raw, dict):
        return TrackingLease()
    try:
        return TrackingLease(
            sequence=max(0, int(raw.get("sequence", 0))),
            expires_at=max(0.0, float(raw.get("expires_at", 0.0))),
            updated_at=max(0.0, float(raw.get("updated_at", 0.0))),
            reason=str(raw.get("reason", ""))[:64],
        )
    except (TypeError, ValueError):
        return TrackingLease()


def read_tracking_lease(path: Path | None = None) -> TrackingLease:
    state_path = path or default_tracking_state_path()
    try:
        return _decode_lease(json.loads(state_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return TrackingLease()


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def signal_face_tracking(
    reason: str,
    *,
    duration: float | None = None,
    path: Path | None = None,
    enabled: bool | None = None,
    now: float | None = None,
) -> bool:
    """Extend the local face-tracking lease without storing conversation data."""

    if enabled is None:
        enabled = env_bool("STACKCHAN_FACE_TRACKING", False)
    if not enabled:
        return False

    state_path = path or default_tracking_state_path()
    current_time = time.time() if now is None else now
    lease_seconds = duration
    if lease_seconds is None:
        lease_seconds = env_float("STACKCHAN_FACE_TRACK_DURATION_SEC", 8.0)
    lease_seconds = max(1.0, min(float(lease_seconds), 30.0))

    with _state_lock(state_path):
        current = read_tracking_lease(state_path)
        lease = TrackingLease(
            sequence=current.sequence + 1,
            expires_at=max(current.expires_at, current_time + lease_seconds),
            updated_at=current_time,
            reason=reason[:64],
        )
        payload = {
            "version": 1,
            "sequence": lease.sequence,
            "expires_at": lease.expires_at,
            "updated_at": lease.updated_at,
            "reason": lease.reason,
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{state_path.name}.",
            dir=state_path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
                handle.write("\n")
            os.replace(temporary_path, state_path)
        finally:
            with suppress(FileNotFoundError):
                temporary_path.unlink()
    return True


@dataclass(frozen=True)
class FaceBox:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0

    @property
    def area(self) -> int:
        return self.width * self.height


def select_face(
    faces: list[FaceBox], previous_center: tuple[float, float] | None = None
) -> FaceBox | None:
    if not faces:
        return None
    if previous_center is None:
        return max(faces, key=lambda face: face.area)

    previous_x, previous_y = previous_center
    return min(
        faces,
        key=lambda face: (
            (face.center[0] - previous_x) ** 2 + (face.center[1] - previous_y) ** 2,
            -face.area,
        ),
    )


@dataclass(frozen=True)
class MotionCommand:
    yaw: float
    pitch: float
    speed: int


@dataclass(frozen=True)
class FaceTrackingSettings:
    fps: float = 2.5
    idle_poll_seconds: float = 0.2
    camera_idle_timeout_ms: int = 5000
    lost_timeout_seconds: float = 1.8
    dead_zone_x: float = 0.14
    dead_zone_y: float = 0.17
    smoothing_alpha: float = 0.4
    yaw_gain: float = 13.0
    pitch_gain: float = 9.0
    yaw_direction: float = -1.0
    pitch_direction: float = -1.0
    max_yaw_step: float = 7.0
    max_pitch_step: float = 5.0
    yaw_limit: float = 55.0
    pitch_min: float = 5.0
    pitch_max: float = 55.0
    min_command_delta: float = 1.0
    min_command_interval: float = 0.3
    acquire_frames: int = 2
    max_frames_per_trigger: int = 0
    speed: int = 18

    @classmethod
    def from_env(cls) -> "FaceTrackingSettings":
        pitch_min = max(
            0.0, min(env_float("STACKCHAN_FACE_TRACK_PITCH_MIN", 5.0), 80.0)
        )
        pitch_max = max(
            pitch_min,
            min(env_float("STACKCHAN_FACE_TRACK_PITCH_MAX", 55.0), 90.0),
        )
        acquire_frames = max(
            1, min(env_int("STACKCHAN_FACE_TRACK_ACQUIRE_FRAMES", 2), 5)
        )
        configured_max_frames = max(
            0, min(env_int("STACKCHAN_FACE_TRACK_MAX_FRAMES", 0), 300)
        )
        max_frames_per_trigger = (
            0
            if configured_max_frames == 0
            else max(acquire_frames, configured_max_frames)
        )
        return cls(
            fps=max(0.5, min(env_float("STACKCHAN_FACE_TRACK_FPS", 2.5), 5.0)),
            idle_poll_seconds=max(
                0.05, min(env_float("STACKCHAN_FACE_TRACK_IDLE_POLL_SEC", 0.2), 2.0)
            ),
            camera_idle_timeout_ms=max(
                1000, min(env_int("STACKCHAN_FACE_TRACK_CAMERA_TIMEOUT_MS", 5000), 15000)
            ),
            lost_timeout_seconds=max(
                0.5, min(env_float("STACKCHAN_FACE_TRACK_LOST_SEC", 1.8), 10.0)
            ),
            dead_zone_x=max(
                0.02, min(env_float("STACKCHAN_FACE_TRACK_DEAD_ZONE_X", 0.14), 0.5)
            ),
            dead_zone_y=max(
                0.02, min(env_float("STACKCHAN_FACE_TRACK_DEAD_ZONE_Y", 0.17), 0.5)
            ),
            smoothing_alpha=max(
                0.05, min(env_float("STACKCHAN_FACE_TRACK_SMOOTHING", 0.4), 1.0)
            ),
            yaw_gain=max(1.0, min(env_float("STACKCHAN_FACE_TRACK_YAW_GAIN", 13.0), 60.0)),
            pitch_gain=max(
                1.0, min(env_float("STACKCHAN_FACE_TRACK_PITCH_GAIN", 9.0), 45.0)
            ),
            yaw_direction=(
                -1.0
                if env_float("STACKCHAN_FACE_TRACK_YAW_DIRECTION", -1.0) < 0
                else 1.0
            ),
            pitch_direction=(
                -1.0
                if env_float("STACKCHAN_FACE_TRACK_PITCH_DIRECTION", -1.0) < 0
                else 1.0
            ),
            max_yaw_step=max(
                1.0, min(env_float("STACKCHAN_FACE_TRACK_MAX_YAW_STEP", 7.0), 25.0)
            ),
            max_pitch_step=max(
                1.0, min(env_float("STACKCHAN_FACE_TRACK_MAX_PITCH_STEP", 5.0), 20.0)
            ),
            yaw_limit=max(
                10.0, min(env_float("STACKCHAN_FACE_TRACK_YAW_LIMIT", 55.0), 128.0)
            ),
            pitch_min=pitch_min,
            pitch_max=pitch_max,
            min_command_delta=max(
                0.1, min(env_float("STACKCHAN_FACE_TRACK_MIN_DELTA", 1.0), 10.0)
            ),
            min_command_interval=max(
                0.05, min(env_float("STACKCHAN_FACE_TRACK_COMMAND_INTERVAL_SEC", 0.3), 2.0)
            ),
            acquire_frames=acquire_frames,
            max_frames_per_trigger=max_frames_per_trigger,
            speed=max(1, min(env_int("STACKCHAN_FACE_TRACK_SPEED", 18), 100)),
        )


class FaceMotionController:
    def __init__(self, settings: FaceTrackingSettings):
        self.settings = settings
        self.reset()

    def reset(self, *, yaw: float = 0.0, pitch: float = 0.0) -> None:
        self.yaw = yaw
        self.pitch = pitch
        self._smoothed_center: tuple[float, float] | None = None
        self._acquired_frames = 0
        self._last_command_at = float("-inf")

    @property
    def smoothed_center(self) -> tuple[float, float] | None:
        return self._smoothed_center

    def observe(
        self,
        face: FaceBox,
        *,
        frame_width: int,
        frame_height: int,
        now: float,
    ) -> MotionCommand | None:
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("frame dimensions must be positive")

        center_x, center_y = face.center
        if self._smoothed_center is None:
            self._smoothed_center = center_x, center_y
        else:
            alpha = self.settings.smoothing_alpha
            previous_x, previous_y = self._smoothed_center
            self._smoothed_center = (
                previous_x + alpha * (center_x - previous_x),
                previous_y + alpha * (center_y - previous_y),
            )

        self._acquired_frames += 1
        if self._acquired_frames < self.settings.acquire_frames:
            return None
        if now - self._last_command_at < self.settings.min_command_interval:
            return None

        smooth_x, smooth_y = self._smoothed_center
        error_x = (smooth_x - frame_width / 2.0) / (frame_width / 2.0)
        error_y = (smooth_y - frame_height / 2.0) / (frame_height / 2.0)

        next_yaw = self.yaw
        next_pitch = self.pitch
        if abs(error_x) > self.settings.dead_zone_x:
            yaw_step = max(
                -self.settings.max_yaw_step,
                min(
                    error_x * self.settings.yaw_gain * self.settings.yaw_direction,
                    self.settings.max_yaw_step,
                ),
            )
            next_yaw = max(-self.settings.yaw_limit, min(self.yaw + yaw_step, self.settings.yaw_limit))
        if abs(error_y) > self.settings.dead_zone_y:
            pitch_step = max(
                -self.settings.max_pitch_step,
                min(
                    error_y * self.settings.pitch_gain * self.settings.pitch_direction,
                    self.settings.max_pitch_step,
                ),
            )
            next_pitch = max(
                self.settings.pitch_min,
                min(self.pitch + pitch_step, self.settings.pitch_max),
            )

        if (
            abs(next_yaw - self.yaw) < self.settings.min_command_delta
            and abs(next_pitch - self.pitch) < self.settings.min_command_delta
        ):
            return None
        return MotionCommand(yaw=next_yaw, pitch=next_pitch, speed=self.settings.speed)

    def accept(self, command: MotionCommand, *, now: float) -> None:
        self.yaw = command.yaw
        self.pitch = command.pitch
        self._last_command_at = now
