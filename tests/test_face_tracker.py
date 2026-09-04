from typing import Any, cast

from mcp_server.face_tracking import (
    FaceBox,
    FaceTrackingSettings,
    read_tracking_lease,
    signal_face_tracking,
)
from scripts.stackchan_face_tracker import FaceTracker


class FakeClient:
    def __init__(self):
        self.moves: list[tuple[float, float, int, bool]] = []
        self.stop_count = 0
        self.home_count = 0

    def start_camera_session(self, idle_timeout_ms: int) -> dict:
        return {"success": True, "idle_timeout_ms": idle_timeout_ms}

    def stop_camera_session(self) -> dict:
        self.stop_count += 1
        return {"success": True}

    def servo_status(self) -> dict:
        return {
            "yaw": {"position": 0},
            "pitch": {"position": 300},
        }

    def snapshot_once(self, *, preview: bool, camera_session: bool):
        assert preview is False
        assert camera_session is True
        return b"jpeg", 4

    def move(
        self,
        yaw: float,
        pitch: float,
        speed: int,
        *,
        interrupt_gesture: bool,
    ) -> dict:
        self.moves.append((yaw, pitch, speed, interrupt_gesture))
        return {"success": True}

    def gesture(self, name: str) -> dict:
        assert name == "home"
        self.home_count += 1
        return {"success": True}


class FakeDetector:
    def __init__(self, faces: list[FaceBox]):
        self.faces = faces

    def detect(self, jpeg_data: bytes):
        assert jpeg_data == b"jpeg"
        return self.faces, 320, 240


def tracker_settings() -> FaceTrackingSettings:
    return FaceTrackingSettings(
        acquire_frames=1,
        max_frames_per_trigger=1,
        smoothing_alpha=1.0,
        min_command_interval=0.0,
    )


def active_lease(state_path):
    assert signal_face_tracking(
        "test", duration=10, path=state_path, enabled=True
    )
    return read_tracking_lease(state_path)


def test_one_frame_mode_releases_camera_and_holds_detected_pose(tmp_path):
    state_path = tmp_path / "tracking.json"
    client = FakeClient()
    tracker = FaceTracker(
        cast(Any, client),
        cast(Any, FakeDetector([FaceBox(240, 90, 50, 50)])),
        tracker_settings(),
        state_path=state_path,
    )
    lease = active_lease(state_path)

    assert tracker._begin(lease)
    tracker._frame(lease)

    assert client.moves
    assert client.stop_count == 1
    assert client.home_count == 0
    assert tracker.camera_active is False
    assert tracker.dormant_sequence == lease.sequence


def test_one_frame_mode_returns_home_when_no_face_is_visible(tmp_path):
    state_path = tmp_path / "tracking.json"
    client = FakeClient()
    tracker = FaceTracker(
        cast(Any, client),
        cast(Any, FakeDetector([])),
        tracker_settings(),
        state_path=state_path,
    )
    lease = active_lease(state_path)

    assert tracker._begin(lease)
    tracker._frame(lease)

    assert client.moves == []
    assert client.stop_count == 1
    assert client.home_count == 1
    assert tracker.dormant_sequence == lease.sequence
