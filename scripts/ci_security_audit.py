"""Repository security checks used by local development and GitHub Actions."""

from __future__ import annotations

import configparser
import ipaddress
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TRACKED_FILES = {
    ".env",
    "firmware/src/config.h",
}

TOKEN_ASSIGNMENT_RE = re.compile(
    r"""(?<![A-Za-z0-9_${])
        (?P<name>FISH_AUDIO_KEY|STACKCHAN_UPLOAD_TOKEN|STACKCHAN_FRONTEND_TOKEN|STACKCHAN_MCP_AUTH_TOKEN)
        \s*[:=]\s*
        (?P<quote>["']?)
        (?P<value>[A-Za-z0-9_./+=:@-]+)
        (?P=quote)
    """,
    re.VERBOSE,
)
TRYCLOUDFLARE_RE = re.compile(r"https?://(?!\.\.\.)([A-Za-z0-9-]+)\.trycloudflare\.com")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ACTION_USE_RE = re.compile(r"^\s*uses:\s*(?P<target>[^#\s]+)", re.MULTILINE)
PINNED_ACTION_RE = re.compile(r"@[0-9a-fA-F]{40}$")

# Retired tunnel hostnames that must never reappear in tracked files. Built from
# split literals below so this module's own source never contains the
# contiguous substring it is checking for.
FORBIDDEN_HOSTNAME_SUBSTRINGS = ("migratory" "bird",)

PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "change-me",
    "dummy",
    "example",
    "example-token",
    "existing-key",
    "fake",
    "fake-key",
    "new-key",
    "placeholder",
    "replace-me",
    "replace-with-your-fish-audio-api-key",
    "token",
    "your-fish-audio-api-key",
    "your-token",
    "your_key_here",
}


def main() -> int:
    errors: list[str] = []
    files = tracked_files()

    for path in files:
        if path.name == "CLAUDE.md":
            continue
        check_forbidden_path(path, errors)
        text = read_text(path)
        if text is None:
            continue
        check_private_ips(path, text, errors)
        check_public_tunnel_urls(path, text, errors)
        check_tokens(path, text, errors)
        check_forbidden_hostnames(path, text, errors)
        if path.match(".github/workflows/*.yml") or path.match(".github/workflows/*.yaml"):
            check_action_pins(path, text, errors)

    check_python_dependency_pins(errors)
    check_platformio_dependency_pins(errors)

    if errors:
        print("Security audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Security audit passed.")
    return 0


def tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable was not found")
    result = subprocess.run(  # noqa: S603 - fixed git argv with resolved executable path.
        [git, "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / name.decode() for name in result.stdout.split(b"\0") if name]


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def check_forbidden_path(path: Path, errors: list[str]) -> None:
    rel = relpath(path)
    if rel in FORBIDDEN_TRACKED_FILES:
        errors.append(f"{rel} must not be tracked")
    if path.name.startswith(".env.") and path.name != ".env.example":
        errors.append(f"{rel} must not be tracked; keep local env files untracked")
    if path.match("deploy/macos/*.plist") and not path.name.endswith(".plist.example"):
        errors.append(f"{rel} must not be tracked; commit only plist examples")


def check_private_ips(path: Path, text: str, errors: list[str]) -> None:
    for match in IPV4_RE.finditer(text):
        value = match.group(0)
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not is_allowed_example_ip(ip):
            errors.append(f"{relpath(path)} contains private LAN IP literal {value}")


def is_allowed_example_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version != 4:
        return False
    ip4 = ipaddress.IPv4Address(ip)
    return (
        ip4 in ipaddress.ip_network("127.0.0.0/8")
        or ip4 == ipaddress.IPv4Address("0.0.0.0")  # noqa: S104 - audit allowlist only.
        or ip4 in ipaddress.ip_network("192.0.2.0/24")
        or ip4 in ipaddress.ip_network("198.51.100.0/24")
        or ip4 in ipaddress.ip_network("203.0.113.0/24")
    )


def check_public_tunnel_urls(path: Path, text: str, errors: list[str]) -> None:
    for match in TRYCLOUDFLARE_RE.finditer(text):
        errors.append(f"{relpath(path)} contains concrete trycloudflare URL {match.group(0)}")


def check_tokens(path: Path, text: str, errors: list[str]) -> None:
    for match in TOKEN_ASSIGNMENT_RE.finditer(text):
        value = match.group("value").strip()
        if value.startswith("$"):
            continue
        if value.lower() not in PLACEHOLDER_VALUES:
            errors.append(f"{relpath(path)} contains non-placeholder {match.group('name')} value")


def check_forbidden_hostnames(path: Path, text: str, errors: list[str]) -> None:
    lowered = text.lower()
    for substring in FORBIDDEN_HOSTNAME_SUBSTRINGS:
        if substring in lowered:
            errors.append(f"{relpath(path)} contains retired tunnel hostname '{substring}...'")


def check_action_pins(path: Path, text: str, errors: list[str]) -> None:
    for match in ACTION_USE_RE.finditer(text):
        target = match.group("target").strip("\"'")
        if target.startswith(("./", "docker://")):
            continue
        if not PINNED_ACTION_RE.search(target):
            errors.append(f"{relpath(path)} has unpinned GitHub Action: {target}")


def check_python_dependency_pins(errors: list[str]) -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        if isinstance(group, list):
            dependencies.extend(item for item in group if isinstance(item, str))

    for dep in dependencies:
        if "==" not in dep:
            errors.append(f"pyproject.toml dependency must use exact == pin: {dep}")


def check_platformio_dependency_pins(errors: list[str]) -> None:
    parser = configparser.ConfigParser()
    parser.read(ROOT / "firmware/platformio.ini", encoding="utf-8")

    for section in parser.sections():
        if parser.has_option(section, "platform"):
            platform = parser.get(section, "platform").strip()
            if platform and "@" not in platform:
                errors.append(f"firmware/platformio.ini {section}.platform must be pinned: {platform}")

        if not parser.has_option(section, "lib_deps"):
            continue
        for dep in parser.get(section, "lib_deps").splitlines():
            dep = dep.strip()
            if not dep or dep.startswith(("#", ";")):
                continue
            if "@" not in dep:
                errors.append(f"firmware/platformio.ini {section}.lib_deps must be pinned: {dep}")


def relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
