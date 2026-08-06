from pathlib import Path

from scripts import ci_security_audit


def test_platformio_versions_must_be_exact() -> None:
    assert ci_security_audit.has_exact_pio_version("vendor/library@1.2.3")
    assert ci_security_audit.has_exact_pio_version("platform@7.0.0")
    assert not ci_security_audit.has_exact_pio_version("vendor/library@^1.2.3")
    assert not ci_security_audit.has_exact_pio_version("vendor/library@latest")
    assert not ci_security_audit.has_exact_pio_version("vendor/library")


def test_python_versions_must_be_exact() -> None:
    assert ci_security_audit.has_exact_python_version("mcp==1.28.1")
    assert ci_security_audit.has_exact_python_version("setuptools==80.9.0")
    assert not ci_security_audit.has_exact_python_version("mcp>=1.28.1")
    assert not ci_security_audit.has_exact_python_version("mcp==1.*")
    assert not ci_security_audit.has_exact_python_version("mcp")


def test_local_macos_paths_reject_real_users_but_allow_template() -> None:
    errors: list[str] = []
    path = ci_security_audit.ROOT / "example.plist"

    ci_security_audit.check_local_macos_paths(path, "/" "Users/alice/project", errors)
    assert errors

    errors.clear()
    ci_security_audit.check_local_macos_paths(
        path,
        "/" "Users/REPLACE_WITH_MACOS_USERNAME/project",
        errors,
    )
    assert errors == []


def test_required_public_files_reports_missing_entries(tmp_path: Path) -> None:
    files = [tmp_path / "LICENSE"]
    errors: list[str] = []

    original_root = ci_security_audit.ROOT
    ci_security_audit.ROOT = tmp_path
    try:
        ci_security_audit.check_required_public_files(files, errors)
    finally:
        ci_security_audit.ROOT = original_root

    assert any("SECURITY.md" in error for error in errors)
