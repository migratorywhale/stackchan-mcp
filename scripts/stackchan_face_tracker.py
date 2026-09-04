#!/usr/bin/env python3
import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_server.face_tracking import (  # noqa: E402
    FaceBox,
    FaceMotionController,
    FaceTrackingSettings,
    TrackingLease,
    acquire_tracker_lock,
    default_tracking_state_path,
    read_tracking_lease,
    select_face,
)
from mcp_server.stackchan_client import StackchanClient  # noqa: E402
from mcp_server.stackchan_config import load_config, load_dotenv  # noqa: E402

logger = logging.getLogger("stackchan.face_tracker")


class OpenCvFaceDetector:
    def __init__(self, *, min_face_pixels: int = 36):
        try:
            import cv2  # pyright: ignore[reportMissingImports]
            import numpy as np  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise RuntimeError(
                "Face tracking needs the optional dependencies; run "
                "`uv sync --extra face-tracking`."
            ) from exc

        self.cv2: Any = cv2
        self.np = np
        cascade_path = Path(self.cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self.detector = self.cv2.CascadeClassifier(str(cascade_path))
        if self.detector.empty():
            raise RuntimeError(f"Could not load OpenCV face cascade: {cascade_path}")
        self.equalizer = self.cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.min_face_pixels = min_face_pixels

    def _detect_boxes(self, gray: Any) -> Any:
        options = {
            "scaleFactor": 1.05,
            "minNeighbors": 5,
            "minSize": (self.min_face_pixels, self.min_face_pixels),
        }
        found = self.detector.detectMultiScale(self.equalizer.apply(gray), **options)
        if len(found) == 0:
            # Global equalization is a useful fallback for strongly backlit faces.
            found = self.detector.detectMultiScale(
                self.cv2.equalizeHist(gray),
                **options,
            )
        return found

    def detect(self, jpeg_data: bytes) -> tuple[list[FaceBox], int, int]:
        encoded = self.np.frombuffer(jpeg_data, dtype=self.np.uint8)
        image = self.cv2.imdecode(encoded, self.cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Camera returned an invalid JPEG")
        gray = self.cv2.cvtColor(image, self.cv2.COLOR_BGR2GRAY)
        found = self._detect_boxes(gray)
        faces = [FaceBox(int(x), int(y), int(width), int(height)) for x, y, width, height in found]
        height, width = gray.shape[:2]
        return faces, width, height


class FaceTracker:
    def __init__(
        self,
        client: StackchanClient,
        detector: OpenCvFaceDetector,
        settings: FaceTrackingSettings,
        *,
        state_path: Path,
    ):
        self.client = client
        self.detector = detector
        self.settings = settings
        self.state_path = state_path
        self.controller = FaceMotionController(settings)
        self.running = True
        self.camera_active = False
        self.active_sequence = 0
        self.dormant_sequence = 0
        self.tracking_started_at = 0.0
        self.last_face_at: float | None = None
        self.previous_face_center: tuple[float, float] | None = None
        self.frames_seen = 0

    def request_stop(self, _signum: int | None = None, _frame: FrameType | None = None) -> None:
        self.running = False

    def _current_pose(self) -> tuple[float, float]:
        try:
            status = self.client.servo_status()
            yaw = float(status.get("yaw", {}).get("position", 0)) / 10.0
            pitch = float(status.get("pitch", {}).get("position", 0)) / 10.0
            return yaw, pitch
        except Exception as exc:
            logger.warning("Could not read initial servo pose; assuming home: %s", exc)
            return 0.0, 0.0

    def _begin(self, lease: TrackingLease) -> bool:
        try:
            result = self.client.start_camera_session(self.settings.camera_idle_timeout_ms)
        except Exception as exc:
            logger.warning("Camera session start failed for trigger %s: %s", lease.reason, exc)
            return False
        if not result.get("success"):
            logger.warning("Camera session rejected for trigger %s: %s", lease.reason, result)
            return False

        yaw, pitch = self._current_pose()
        self.controller.reset(yaw=yaw, pitch=pitch)
        self.camera_active = True
        self.active_sequence = lease.sequence
        self.tracking_started_at = time.monotonic()
        self.last_face_at = None
        self.previous_face_center = None
        self.frames_seen = 0
        logger.info("Tracking started: sequence=%d reason=%s", lease.sequence, lease.reason)
        return True

    def _home(self) -> None:
        try:
            result = self.client.gesture("home")
            if not result.get("success"):
                logger.warning("Home command was rejected: %s", result)
        except Exception as exc:
            logger.warning("Home command failed: %s", exc)

    def _end(self, *, home: bool, dormant_sequence: int = 0) -> None:
        if self.camera_active:
            try:
                result = self.client.stop_camera_session()
                if not result.get("success"):
                    logger.warning("Camera session stop was rejected: %s", result)
            except Exception as exc:
                logger.warning("Camera session stop failed; firmware watchdog will recover it: %s", exc)
        self.camera_active = False
        if home:
            self._home()
        self.controller.reset()
        self.previous_face_center = None
        self.last_face_at = None
        self.frames_seen = 0
        self.dormant_sequence = dormant_sequence

    def _frame_budget_reached(self) -> bool:
        limit = self.settings.max_frames_per_trigger
        return limit > 0 and self.frames_seen >= limit

    def _frame(self, lease: TrackingLease) -> None:
        try:
            jpeg_data, _size = self.client.snapshot_once(
                preview=False,
                camera_session=True,
            )
            if jpeg_data is None:
                raise RuntimeError("camera frame request was rejected")
            faces, width, height = self.detector.detect(jpeg_data)
            logger.debug("Tracking frame: %dx%d faces=%d", width, height, len(faces))
        except Exception as exc:
            logger.warning("Tracking frame failed: %s", exc)
            self._end(home=True, dormant_sequence=lease.sequence)
            return

        now = time.monotonic()
        self.frames_seen += 1

        if not read_tracking_lease(self.state_path).active():
            logger.info("Tracking lease expired while the frame was in transit")
            self._end(home=True)
            return

        face = select_face(faces, self.previous_face_center)
        if face is None:
            absent_since = self.last_face_at or self.tracking_started_at
            if (
                self._frame_budget_reached()
                or now - absent_since >= self.settings.lost_timeout_seconds
            ):
                logger.info("No face visible; returning home and sleeping until the next trigger")
                self._end(home=True, dormant_sequence=lease.sequence)
            return

        self.last_face_at = now
        self.previous_face_center = face.center
        command = self.controller.observe(
            face,
            frame_width=width,
            frame_height=height,
            now=now,
        )
        if command is None:
            if self._frame_budget_reached():
                logger.info("Tracking frame budget reached; holding the current pose")
                self._end(home=False, dormant_sequence=lease.sequence)
            return

        try:
            result = self.client.move(
                command.yaw,
                command.pitch,
                command.speed,
                interrupt_gesture=False,
            )
        except Exception as exc:
            logger.warning("Face-tracking move failed: %s", exc)
            return
        if result.get("success"):
            self.controller.accept(command, now=now)
            logger.info(
                "Tracking move: yaw=%.1f pitch=%.1f speed=%d",
                command.yaw,
                command.pitch,
                command.speed,
            )
        elif result.get("error") != "gesture active":
            logger.warning("Face-tracking move was rejected: %s", result)
        if self._frame_budget_reached():
            logger.info("Tracking frame budget reached; holding the current pose")
            self._end(home=False, dormant_sequence=lease.sequence)

    def run(self, *, once: bool = False) -> int:
        frame_interval = 1.0 / self.settings.fps
        while self.running:
            lease = read_tracking_lease(self.state_path)
            if not lease.active():
                if self.camera_active:
                    logger.info("Tracking lease expired")
                    self._end(home=True)
                if once:
                    return 0
                time.sleep(self.settings.idle_poll_seconds)
                continue

            if lease.sequence == self.dormant_sequence:
                if once:
                    return 0
                time.sleep(self.settings.idle_poll_seconds)
                continue

            if not self.camera_active and not self._begin(lease):
                if once:
                    return 1
                time.sleep(1.0)
                continue
            self.active_sequence = lease.sequence

            frame_started = time.monotonic()
            self._frame(lease)
            if once:
                self._end(home=False)
                return 0
            elapsed = time.monotonic() - frame_started
            time.sleep(max(0.01, frame_interval - elapsed))

        self._end(home=True)
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track the largest visible face with Stack-chan while a local activity lease is active."
    )
    parser.add_argument("--once", action="store_true", help="Process at most one active frame, then exit")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Override the local tracking lease path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state_path = args.state or default_tracking_state_path()
    tracker_lock = acquire_tracker_lock(state_path)
    if tracker_lock is None:
        logger.error("Another face tracker already owns %s", state_path)
        return 2
    detector = OpenCvFaceDetector()
    tracker = FaceTracker(
        StackchanClient(load_config()),
        detector,
        FaceTrackingSettings.from_env(),
        state_path=state_path,
    )
    signal.signal(signal.SIGINT, tracker.request_stop)
    signal.signal(signal.SIGTERM, tracker.request_stop)
    try:
        return tracker.run(once=args.once)
    finally:
        tracker_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
