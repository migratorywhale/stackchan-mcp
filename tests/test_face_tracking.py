import json

import pytest

from mcp_server.face_tracking import (
    FaceBox,
    FaceMotionController,
    FaceTrackingSettings,
    read_tracking_lease,
    select_face,
    signal_face_tracking,
)


def settings(**overrides) -> FaceTrackingSettings:
    values = {
        "acquire_frames": 1,
        "smoothing_alpha": 1.0,
        "min_command_interval": 0.0,
    }
    values.update(overrides)
    return FaceTrackingSettings(**values)


def test_signal_face_tracking_is_disabled_by_default(monkeypatch, tmp_path):
    state_path = tmp_path / "tracking.json"
    monkeypatch.delenv("STACKCHAN_FACE_TRACKING", raising=False)

    assert not signal_face_tracking("wake_word", path=state_path, now=100.0)
    assert not state_path.exists()


def test_signal_face_tracking_extends_lease_and_increments_sequence(tmp_path):
    state_path = tmp_path / "tracking.json"

    assert signal_face_tracking(
        "wake_word", duration=8.0, path=state_path, enabled=True, now=100.0
    )
    assert signal_face_tracking(
        "stackchan_say", duration=4.0, path=state_path, enabled=True, now=102.0
    )

    lease = read_tracking_lease(state_path)
    assert lease.sequence == 2
    assert lease.reason == "stackchan_say"
    assert lease.expires_at == 108.0
    assert json.loads(state_path.read_text())["version"] == 1


def test_face_tracking_settings_expand_frame_budget_to_acquisition(monkeypatch):
    monkeypatch.setenv("STACKCHAN_FACE_TRACK_ACQUIRE_FRAMES", "3")
    monkeypatch.setenv("STACKCHAN_FACE_TRACK_MAX_FRAMES", "1")

    configured = FaceTrackingSettings.from_env()

    assert configured.acquire_frames == 3
    assert configured.max_frames_per_trigger == 3


def test_face_tracking_settings_allow_unbounded_frame_budget(monkeypatch):
    monkeypatch.setenv("STACKCHAN_FACE_TRACK_MAX_FRAMES", "0")

    assert FaceTrackingSettings.from_env().max_frames_per_trigger == 0


def test_select_face_prefers_largest_then_stays_near_previous_target():
    left = FaceBox(10, 20, 50, 50)
    right = FaceBox(210, 20, 70, 70)

    assert select_face([left, right]) == right
    assert select_face([left, right], previous_center=left.center) == left
    assert select_face([]) is None


def test_motion_controller_uses_dead_zone():
    controller = FaceMotionController(settings())

    command = controller.observe(
        FaceBox(130, 90, 60, 60),
        frame_width=320,
        frame_height=240,
        now=1.0,
    )

    assert command is None


def test_motion_controller_turns_right_for_face_on_right():
    controller = FaceMotionController(settings())

    command = controller.observe(
        FaceBox(240, 90, 50, 50),
        frame_width=320,
        frame_height=240,
        now=1.0,
    )

    assert command is not None
    assert command.yaw < 0
    assert command.pitch == 0


def test_motion_controller_looks_up_for_face_above_center():
    controller = FaceMotionController(settings())

    command = controller.observe(
        FaceBox(135, 10, 50, 50),
        frame_width=320,
        frame_height=240,
        now=1.0,
    )

    assert command is not None
    assert command.yaw == 0
    assert command.pitch > 0


def test_motion_controller_can_flip_both_servo_directions():
    controller = FaceMotionController(
        settings(yaw_direction=1.0, pitch_direction=1.0)
    )
    controller.reset(pitch=30.0)

    command = controller.observe(
        FaceBox(240, 10, 50, 50),
        frame_width=320,
        frame_height=240,
        now=1.0,
    )

    assert command is not None
    assert command.yaw > 0
    assert command.pitch < 30.0


def test_motion_controller_rejects_invalid_frame_dimensions():
    controller = FaceMotionController(settings())

    with pytest.raises(ValueError, match="frame dimensions"):
        controller.observe(
            FaceBox(0, 0, 10, 10),
            frame_width=0,
            frame_height=240,
            now=1.0,
        )


def test_motion_controller_limits_each_step_and_waits_for_acquisition():
    controller = FaceMotionController(
        settings(acquire_frames=2, max_yaw_step=3.0, yaw_gain=60.0)
    )
    face = FaceBox(270, 90, 40, 40)

    assert (
        controller.observe(face, frame_width=320, frame_height=240, now=1.0) is None
    )
    command = controller.observe(face, frame_width=320, frame_height=240, now=1.5)

    assert command is not None
    assert command.yaw == pytest.approx(-3.0)
    controller.accept(command, now=1.5)
    assert controller.yaw == pytest.approx(-3.0)
