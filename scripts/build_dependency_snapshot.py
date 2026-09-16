#!/usr/bin/env python3
"""Build a GitHub dependency-submission snapshot from uv.lock."""

from __future__ import annotations

import argparse
import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

DETECTOR_NAME = "stackchan-uv-lock"
DETECTOR_VERSION = "2"
PYPI_REGISTRY = "https://pypi.org/simple"

PackageKey = tuple[str, str]
DependencyRequest = tuple[PackageKey, frozenset[str]]


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def package_url(name: str, version: str) -> str:
    normalized = normalize_package_name(name)
    return f"pkg:pypi/{quote(normalized, safe='-._~')}@{quote(version, safe='')}"


def dependency_requests(
    entries: Iterable[Any], packages: Mapping[PackageKey, dict[str, Any]]
) -> list[DependencyRequest]:
    # uv.lock is universal. Keep marker-gated entries so the snapshot covers every
    # supported platform, matching GitHub's native dependency-graph inventory.
    requests: list[DependencyRequest] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            raise ValueError(f"unsupported uv.lock dependency entry: {entry!r}")

        name = normalize_package_name(entry["name"])
        extras = entry.get("extra", [])
        if not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras):
            raise ValueError(f"unsupported uv.lock dependency extras: {extras!r}")
        source = entry.get("source", {})
        if not isinstance(source, Mapping):
            raise ValueError(f"unsupported uv.lock dependency source: {source!r}")
        if source:
            if "registry" not in source:
                continue
            registry = source["registry"]
            if not isinstance(registry, str) or registry.rstrip("/") != PYPI_REGISTRY:
                raise ValueError(f"unsupported registry for {name}: {source['registry']}")

        candidates = [key for key in packages if key[0] == name]
        if "version" in entry:
            candidates = [key for key in candidates if key[1] == str(entry["version"])]
        if not candidates:
            if source or "version" in entry or any(key[0] == name for key in packages):
                raise ValueError(f"unresolved registry dependency: {entry!r}")
            # Non-registry packages (including the editable root) are not submitted.
            continue
        if len(candidates) != 1:
            raise ValueError(f"ambiguous dependency {name!r}: a locked version is required")
        requests.append(
            (candidates[0], frozenset(normalize_package_name(extra) for extra in extras))
        )
    return requests


def _find_root_package(packages: list[dict[str, Any]]) -> dict[str, Any]:
    roots = [package for package in packages if package.get("source", {}).get("editable") == "."]
    if len(roots) != 1:
        raise ValueError(f"expected one editable root package, found {len(roots)}")
    return roots[0]


def _registry_packages(packages: list[dict[str, Any]]) -> dict[PackageKey, dict[str, Any]]:
    by_key: dict[PackageKey, dict[str, Any]] = {}
    for package in packages:
        source = package.get("source", {})
        if "registry" not in source:
            continue
        if source["registry"].rstrip("/") != PYPI_REGISTRY:
            raise ValueError(
                f"unsupported registry for {package.get('name')}: {source['registry']}"
            )

        key = (normalize_package_name(package["name"]), str(package["version"]))
        if key in by_key:
            raise ValueError(f"duplicate locked distribution: {key!r}")
        by_key[key] = package
    return by_key


def _walk_dependencies(
    seeds: Iterable[DependencyRequest],
    packages: Mapping[PackageKey, dict[str, Any]],
) -> dict[PackageKey, set[str]]:
    activated_extras: dict[PackageKey, set[str]] = {}
    pending = list(seeds)
    while pending:
        key, requested_extras = pending.pop()
        first_visit = key not in activated_extras
        known_extras = activated_extras.setdefault(key, set())
        new_extras = set(requested_extras) - known_extras
        if not first_visit and not new_extras:
            continue

        package = packages[key]
        if first_visit:
            pending.extend(dependency_requests(package.get("dependencies", []), packages))
        optional_dependencies = package.get("optional-dependencies", {})
        for extra in new_extras:
            pending.extend(dependency_requests(optional_dependencies.get(extra, []), packages))
        known_extras.update(requested_extras)
    return activated_extras


def _activated_dependency_keys(
    package: Mapping[str, Any],
    extras: Iterable[str],
    packages: Mapping[PackageKey, dict[str, Any]],
) -> set[PackageKey]:
    requests = dependency_requests(package.get("dependencies", []), packages)
    optional_dependencies = package.get("optional-dependencies", {})
    for extra in extras:
        requests.extend(dependency_requests(optional_dependencies.get(extra, []), packages))
    return {key for key, _extras in requests}


def build_manifest(
    lock_data: Mapping[str, Any], source_location: str = "uv.lock"
) -> dict[str, Any]:
    packages = list(lock_data.get("package", []))
    root = _find_root_package(packages)
    registry_packages = _registry_packages(packages)

    runtime_seeds = dependency_requests(root.get("dependencies", []), registry_packages)
    for dependencies in root.get("optional-dependencies", {}).values():
        runtime_seeds.extend(dependency_requests(dependencies, registry_packages))
    direct_runtime = {key for key, _extras in runtime_seeds}

    development_seeds: list[DependencyRequest] = []
    for dependencies in root.get("dev-dependencies", {}).values():
        development_seeds.extend(dependency_requests(dependencies, registry_packages))
    direct_development = {key for key, _extras in development_seeds}

    runtime = _walk_dependencies(runtime_seeds, registry_packages)
    development = _walk_dependencies(development_seeds, registry_packages)
    included = set(runtime) | set(development)
    direct = direct_runtime | direct_development

    purls = {key: package_url(*key) for key in included}
    resolved: dict[str, Any] = {}
    for key in sorted(included):
        package = registry_packages[key]
        purl = purls[key]
        extras = runtime.get(key, set()) | development.get(key, set())
        child_purls = sorted(
            purls[child]
            for child in _activated_dependency_keys(package, extras, registry_packages)
            if child in purls
        )
        resolved[purl] = {
            "package_url": purl,
            "relationship": "direct" if key in direct else "indirect",
            "scope": "runtime" if key in runtime else "development",
            "dependencies": child_purls,
        }

    return {
        "name": source_location,
        "file": {"source_location": source_location},
        "metadata": {
            "uv_lock_version": str(lock_data.get("version", "unknown")),
            "uv_lock_revision": str(lock_data.get("revision", "unknown")),
        },
        "resolved": resolved,
    }


def build_snapshot(
    lock_data: Mapping[str, Any],
    *,
    sha: str,
    ref: str,
    job_id: str,
    correlator: str,
    detector_url: str,
    job_url: str | None = None,
    scanned: str | None = None,
    source_location: str = "uv.lock",
) -> dict[str, Any]:
    job = {"id": job_id, "correlator": correlator}
    if job_url:
        job["html_url"] = job_url
    return {
        "version": 0,
        "sha": sha,
        "ref": ref,
        "job": job,
        "detector": {
            "name": DETECTOR_NAME,
            "version": DETECTOR_VERSION,
            "url": detector_url,
        },
        "scanned": scanned or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "manifests": {source_location: build_manifest(lock_data, source_location)},
    }


def required_value(value: str | None, label: str) -> str:
    if value:
        return value
    raise SystemExit(f"missing {label}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF"))
    parser.add_argument("--job-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--correlator", default=os.environ.get("GITHUB_JOB"))
    parser.add_argument("--job-url", default="")
    parser.add_argument("--detector-url", default="")
    args = parser.parse_args()

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    detector_url = args.detector_url or (f"{server_url}/{repository}" if repository else "")
    job_url = args.job_url
    if not job_url and repository and os.environ.get("GITHUB_RUN_ID"):
        job_url = f"{server_url}/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    lock_data = tomllib.loads(args.lock.read_text())
    snapshot = build_snapshot(
        lock_data,
        sha=required_value(args.sha, "commit SHA"),
        ref=required_value(args.ref, "git ref"),
        job_id=required_value(args.job_id, "job id"),
        correlator=required_value(args.correlator, "job correlator"),
        detector_url=required_value(detector_url, "detector URL"),
        job_url=job_url or None,
        source_location=args.lock.as_posix(),
    )
    args.output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
