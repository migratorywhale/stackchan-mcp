import pytest

from scripts.build_dependency_snapshot import build_manifest, build_snapshot


def sample_lock() -> dict:
    registry = {"registry": "https://pypi.org/simple"}
    return {
        "version": 1,
        "revision": 3,
        "package": [
            {
                "name": "demo",
                "version": "0.1.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "MCP"}],
                "optional-dependencies": {"camera": [{"name": "Pillow"}]},
                "dev-dependencies": {"dev": [{"name": "pytest"}]},
            },
            {
                "name": "mcp",
                "version": "1.28.1",
                "source": registry,
                "dependencies": [
                    {"name": "Starlette"},
                    {"name": "PyJWT", "extra": ["crypto"]},
                    {"name": "pywin32", "marker": "sys_platform == 'win32'"},
                ],
            },
            {"name": "starlette", "version": "1.3.1", "source": registry},
            {
                "name": "pyjwt",
                "version": "2.13.0",
                "source": registry,
                "optional-dependencies": {"crypto": [{"name": "cryptography"}]},
            },
            {"name": "cryptography", "version": "50.0.0", "source": registry},
            {"name": "pywin32", "version": "311", "source": registry},
            {"name": "pillow", "version": "12.0.0", "source": registry},
            {
                "name": "pytest",
                "version": "9.0.3",
                "source": registry,
                "dependencies": [{"name": "pluggy"}],
            },
            {"name": "pluggy", "version": "1.6.0", "source": registry},
            {"name": "orphan", "version": "1.0.0", "source": registry},
        ],
    }


def test_build_manifest_marks_runtime_development_and_transitive_packages() -> None:
    manifest = build_manifest(sample_lock())
    resolved = manifest["resolved"]

    assert set(resolved) == {
        "pkg:pypi/cryptography@50.0.0",
        "pkg:pypi/mcp@1.28.1",
        "pkg:pypi/pillow@12.0.0",
        "pkg:pypi/pluggy@1.6.0",
        "pkg:pypi/pyjwt@2.13.0",
        "pkg:pypi/pywin32@311",
        "pkg:pypi/pytest@9.0.3",
        "pkg:pypi/starlette@1.3.1",
    }
    assert resolved["pkg:pypi/mcp@1.28.1"] == {
        "package_url": "pkg:pypi/mcp@1.28.1",
        "relationship": "direct",
        "scope": "runtime",
        "dependencies": [
            "pkg:pypi/pyjwt@2.13.0",
            "pkg:pypi/pywin32@311",
            "pkg:pypi/starlette@1.3.1",
        ],
    }
    assert resolved["pkg:pypi/pyjwt@2.13.0"]["dependencies"] == [
        "pkg:pypi/cryptography@50.0.0"
    ]
    assert resolved["pkg:pypi/cryptography@50.0.0"]["scope"] == "runtime"
    assert resolved["pkg:pypi/pywin32@311"]["scope"] == "runtime"
    assert resolved["pkg:pypi/starlette@1.3.1"]["relationship"] == "indirect"
    assert resolved["pkg:pypi/starlette@1.3.1"]["scope"] == "runtime"
    assert resolved["pkg:pypi/pytest@9.0.3"]["relationship"] == "direct"
    assert resolved["pkg:pypi/pytest@9.0.3"]["scope"] == "development"
    assert resolved["pkg:pypi/pluggy@1.6.0"]["relationship"] == "indirect"
    assert resolved["pkg:pypi/pluggy@1.6.0"]["scope"] == "development"
    assert resolved["pkg:pypi/pillow@12.0.0"]["relationship"] == "direct"
    assert resolved["pkg:pypi/pillow@12.0.0"]["scope"] == "runtime"


def test_build_manifest_rejects_ambiguous_normalized_names() -> None:
    lock = sample_lock()
    lock["package"].append(
        {
            "name": "starlette",
            "version": "2.0.0",
            "source": {"registry": "https://pypi.org/simple"},
        }
    )

    with pytest.raises(ValueError, match="multiple locked distributions"):
        build_manifest(lock)


def test_build_snapshot_wraps_manifest_with_github_metadata() -> None:
    snapshot = build_snapshot(
        sample_lock(),
        sha="a" * 40,
        ref="refs/heads/master",
        job_id="123.1",
        correlator="security_dependency-submission",
        detector_url="https://github.com/example/demo",
        job_url="https://github.com/example/demo/actions/runs/123",
        scanned="2026-08-30T00:00:00Z",
    )

    assert snapshot["sha"] == "a" * 40
    assert snapshot["ref"] == "refs/heads/master"
    assert snapshot["job"] == {
        "id": "123.1",
        "correlator": "security_dependency-submission",
        "html_url": "https://github.com/example/demo/actions/runs/123",
    }
    assert snapshot["detector"]["name"] == "stackchan-uv-lock"
    assert snapshot["scanned"] == "2026-08-30T00:00:00Z"
    assert "uv.lock" in snapshot["manifests"]
