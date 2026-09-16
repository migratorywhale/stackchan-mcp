import tomllib
from pathlib import Path

import pytest

from scripts.build_dependency_snapshot import build_manifest, build_snapshot, package_url


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
    assert resolved["pkg:pypi/pyjwt@2.13.0"]["dependencies"] == ["pkg:pypi/cryptography@50.0.0"]
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

    with pytest.raises(ValueError, match="ambiguous dependency"):
        build_manifest(lock)


def versioned_lock() -> dict:
    registry = {"registry": "https://pypi.org/simple"}
    return {
        "version": 1,
        "package": [
            {
                "name": "demo",
                "source": {"editable": "."},
                "dependencies": [{"name": "adapter"}],
                "dev-dependencies": {
                    "dev": [{"name": "shared-lib", "version": "2.0", "source": registry}]
                },
            },
            {
                "name": "adapter",
                "version": "1.0",
                "source": registry,
                "dependencies": [
                    {
                        "name": "Shared_Lib",
                        "version": "1.0",
                        "source": registry,
                        "extra": ["TLS"],
                        "marker": "python_full_version < '3.12'",
                    }
                ],
            },
            {
                "name": "shared-lib",
                "version": "1.0",
                "source": registry,
                "optional-dependencies": {"tls": [{"name": "old-tls"}]},
            },
            {
                "name": "shared-lib",
                "version": "2.0",
                "source": registry,
                "dependencies": [{"name": "new-child"}],
                "optional-dependencies": {"tls": [{"name": "new-tls"}]},
            },
            {"name": "old-tls", "version": "1.0", "source": registry},
            {"name": "new-tls", "version": "2.0", "source": registry},
            {"name": "new-child", "version": "1.0", "source": registry},
        ],
    }


def test_versioned_edges_keep_scope_relationship_and_extras_separate() -> None:
    resolved = build_manifest(versioned_lock())["resolved"]

    assert resolved["pkg:pypi/adapter@1.0"]["dependencies"] == ["pkg:pypi/shared-lib@1.0"]
    assert resolved["pkg:pypi/shared-lib@1.0"] == {
        "package_url": "pkg:pypi/shared-lib@1.0",
        "relationship": "indirect",
        "scope": "runtime",
        "dependencies": ["pkg:pypi/old-tls@1.0"],
    }
    assert resolved["pkg:pypi/shared-lib@2.0"] == {
        "package_url": "pkg:pypi/shared-lib@2.0",
        "relationship": "direct",
        "scope": "development",
        "dependencies": ["pkg:pypi/new-child@1.0"],
    }
    assert resolved["pkg:pypi/new-child@1.0"]["scope"] == "development"
    assert "pkg:pypi/new-tls@2.0" not in resolved


def test_all_marker_branches_are_included_without_activating_other_version_extras() -> None:
    lock = versioned_lock()
    lock["package"][1]["dependencies"].append(
        {
            "name": "shared-lib",
            "version": "2.0",
            "source": {"registry": "https://pypi.org/simple/"},
            "marker": "python_full_version >= '3.12'",
        }
    )
    resolved = build_manifest(lock)["resolved"]

    assert resolved["pkg:pypi/adapter@1.0"]["dependencies"] == [
        "pkg:pypi/shared-lib@1.0",
        "pkg:pypi/shared-lib@2.0",
    ]
    assert resolved["pkg:pypi/shared-lib@2.0"]["scope"] == "runtime"
    assert resolved["pkg:pypi/new-child@1.0"]["scope"] == "runtime"
    assert "pkg:pypi/new-tls@2.0" not in resolved


def test_unreferenced_version_is_not_reported() -> None:
    lock = versioned_lock()
    lock["package"][0].pop("dev-dependencies")
    resolved = build_manifest(lock)["resolved"]
    assert "pkg:pypi/shared-lib@1.0" in resolved
    assert "pkg:pypi/shared-lib@2.0" not in resolved
    assert "pkg:pypi/new-child@1.0" not in resolved


def test_versioned_cycles_and_later_extra_activation_terminate() -> None:
    lock = versioned_lock()
    lock["package"][3]["dependencies"].append(
        {"name": "shared-lib", "version": "1.0", "extra": ["tls"]}
    )
    lock["package"][2]["dependencies"] = [{"name": "shared-lib", "version": "2.0"}]
    resolved = build_manifest(lock)["resolved"]
    assert resolved["pkg:pypi/shared-lib@1.0"]["dependencies"] == [
        "pkg:pypi/old-tls@1.0",
        "pkg:pypi/shared-lib@2.0",
    ]
    assert resolved["pkg:pypi/old-tls@1.0"]["scope"] == "runtime"


def test_duplicate_normalized_name_and_version_is_rejected() -> None:
    lock = versioned_lock()
    lock["package"].append(
        {
            "name": "Shared_Lib",
            "version": "1.0",
            "source": {"registry": "https://pypi.org/simple"},
        }
    )
    with pytest.raises(ValueError, match="duplicate locked distribution"):
        build_manifest(lock)


def test_missing_version_is_not_silently_replaced() -> None:
    lock = versioned_lock()
    lock["package"][1]["dependencies"][0]["version"] = "99.0"
    with pytest.raises(ValueError, match="unresolved registry dependency"):
        build_manifest(lock)


def test_dependency_source_cannot_be_relabelled_as_pypi() -> None:
    lock = versioned_lock()
    lock["package"][1]["dependencies"][0]["source"] = {"registry": "https://example.invalid/simple"}
    with pytest.raises(ValueError, match="unsupported registry"):
        build_manifest(lock)


def test_non_registry_edge_does_not_activate_same_named_pypi_package() -> None:
    lock = versioned_lock()
    lock["package"][1]["dependencies"][0]["source"] = {"editable": "./shared-lib"}
    resolved = build_manifest(lock)["resolved"]
    assert "pkg:pypi/shared-lib@1.0" not in resolved
    assert resolved["pkg:pypi/adapter@1.0"]["dependencies"] == []


def test_lock_order_does_not_change_snapshot() -> None:
    lock = versioned_lock()
    expected = build_manifest(lock)
    lock["package"].reverse()
    assert build_manifest(lock) == expected


@pytest.mark.parametrize("source", ["pypi", {"registry": 123}])
def test_invalid_dependency_source_is_rejected(source: object) -> None:
    lock = versioned_lock()
    lock["package"][1]["dependencies"][0]["source"] = source
    with pytest.raises(ValueError, match="unsupported"):
        build_manifest(lock)


def test_repository_lock_builds_complete_snapshot() -> None:
    lock = tomllib.loads((Path(__file__).resolve().parents[1] / "uv.lock").read_text())
    resolved = build_manifest(lock)["resolved"]
    registry_packages = [p for p in lock["package"] if "registry" in p["source"]]
    assert set(resolved) == {package_url(p["name"], p["version"]) for p in registry_packages}
    numpy_purls = {
        package_url(p["name"], p["version"]) for p in registry_packages if p["name"] == "numpy"
    }
    opencv = next(p for p in registry_packages if p["name"] == "opencv-python-headless")
    assert numpy_purls
    assert (
        set(resolved[package_url(opencv["name"], opencv["version"])]["dependencies"]) == numpy_purls
    )
    assert all(
        child in resolved for package in resolved.values() for child in package["dependencies"]
    )


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
