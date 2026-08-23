import signal
import subprocess
from dataclasses import dataclass

import pytest

from mcp_server.audio_publish import publish_wav
from mcp_server.mcp_tools import register_tools
from mcp_server.stackchan_config import load_config


def test_publish_wav_waits_for_one_file(monkeypatch, tmp_path):
    wav_path = tmp_path / "speech.wav"
    wav_path.write_bytes(b"RIFF")
    calls = []

    @dataclass
    class FakeProcess:
        pid: int = 100
        returncode: int = 0

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            return "", ""

    monkeypatch.setattr("mcp_server.audio_publish.shutil.which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr(
        "mcp_server.audio_publish.subprocess.Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or FakeProcess(),
    )

    publish_wav(wav_path, "macbook-isa:stackchan_audio", 12.5)

    command, kwargs = calls[0]
    assert command[0] == "/usr/bin/rsync"
    assert command[1:3] == ["-a", "--timeout=13"]
    assert command[-2:] == [str(wav_path), "macbook-isa:stackchan_audio/"]
    assert kwargs == {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "start_new_session": True,
    }
    assert calls[1] == ("communicate", 12.5)


def test_publish_wav_retries_timeout_and_terminates_process_group(monkeypatch, tmp_path):
    wav_path = tmp_path / "speech.wav"
    wav_path.write_bytes(b"RIFF")
    monkeypatch.setattr("mcp_server.audio_publish.shutil.which", lambda _name: "/usr/bin/rsync")
    calls = []

    class FakeProcess:
        def __init__(self, attempt):
            self.attempt = attempt
            self.pid = 100 + attempt
            self.returncode = 0
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            calls.append(("communicate", self.attempt, timeout))
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["rsync"], 3)
            return "", ""

    processes = []

    def fake_popen(*_args, **_kwargs):
        process = FakeProcess(len(processes) + 1)
        processes.append(process)
        return process

    monkeypatch.setattr("mcp_server.audio_publish.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "mcp_server.audio_publish.os.killpg",
        lambda pid, sig: calls.append(("killpg", pid, sig)),
    )
    monkeypatch.setattr(
        "mcp_server.audio_publish.time.sleep",
        lambda delay: calls.append(("sleep", delay)),
    )

    with pytest.raises(RuntimeError, match=r"audio publish timed out after 2 attempts \(3s each\)"):
        publish_wav(wav_path, "macbook-isa:stackchan_audio/", 3)

    assert len(processes) == 2
    assert ("killpg", 101, signal.SIGTERM) in calls
    assert ("killpg", 102, signal.SIGTERM) in calls
    assert ("sleep", 0.5) in calls


def test_publish_wav_succeeds_on_retry(monkeypatch, tmp_path):
    wav_path = tmp_path / "speech.wav"
    wav_path.write_bytes(b"RIFF")
    monkeypatch.setattr("mcp_server.audio_publish.shutil.which", lambda _name: "/usr/bin/rsync")
    processes = []

    class FakeProcess:
        returncode = 0

        def __init__(self, attempt):
            self.attempt = attempt
            self.pid = 200 + attempt
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.attempt == 1 and self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["rsync"], timeout)
            return "", ""

    def fake_popen(*_args, **_kwargs):
        process = FakeProcess(len(processes) + 1)
        processes.append(process)
        return process

    monkeypatch.setattr("mcp_server.audio_publish.subprocess.Popen", fake_popen)
    monkeypatch.setattr("mcp_server.audio_publish.os.killpg", lambda *_args: None)
    monkeypatch.setattr("mcp_server.audio_publish.time.sleep", lambda _delay: None)

    publish_wav(wav_path, "macbook-isa:stackchan_audio/", 3)

    assert len(processes) == 2


def test_audio_publish_config_is_optional_and_read_from_env(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)
    monkeypatch.setenv("STACKCHAN_AUDIO_PUBLISH_TARGET", "macbook-isa:stackchan_audio/")
    monkeypatch.setenv("STACKCHAN_AUDIO_PUBLISH_TIMEOUT_SEC", "17")
    monkeypatch.setenv("STACKCHAN_AUDIO_PUBLISH_ATTEMPTS", "3")

    config = load_config()

    assert config.audio_publish_target == "macbook-isa:stackchan_audio/"
    assert config.audio_publish_timeout == 17
    assert config.audio_publish_attempts == 3


def test_stackchan_say_publishes_before_play(monkeypatch, tmp_path):
    wav_path = tmp_path / "speech.wav"
    wav_path.write_bytes(b"RIFF")
    events = []

    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, **_kwargs):
            def decorator(func):
                self.tools[func.__name__] = func
                return func

            return decorator

    class FakeClient:
        def playback_status(self):
            return {"playing": False, "started_ms": 1}

        def play(self, _url):
            events.append("play")
            return {"success": True}

        def wait_for_playback_start(self, baseline_started_ms=None):
            return {"started": True, "status": {"started_ms": baseline_started_ms}}

    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)
    monkeypatch.setenv("STACKCHAN_AUDIO_MODE", "wav")
    monkeypatch.setenv("STACKCHAN_AUDIO_PUBLISH_TARGET", "macbook-isa:stackchan_audio/")
    monkeypatch.setattr("mcp_server.mcp_tools.start_audio_server", lambda _port: None)
    monkeypatch.setattr("mcp_server.audio_processing.generate_tts", lambda *_args, **_kwargs: wav_path)
    monkeypatch.setattr("mcp_server.audio_processing.validate_playback_wav", lambda _path: None)
    monkeypatch.setattr(
        "mcp_server.mcp_tools.publish_wav",
        lambda *_args, **_kwargs: events.append("publish"),
    )

    mcp = FakeMcp()
    register_tools(mcp, FakeClient(), load_config(), image_cls=None)

    assert "Stack-chan is saying" in mcp.tools["stackchan_say"]("hello", lang="en")
    assert events == ["publish", "play"]
