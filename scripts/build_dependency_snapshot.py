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
DETECTOR_VERSION = "1"
PYPI_REGISTRY = "https://pypi.org/simple"


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def package_url(name: str, version: str) -> str:
    normalized = normalize_package_name(name)
    return f"pkg:pypi/{quote(normalized, safe='-._~')}@{quote(version, safe='')}"


def dependency_requests(entries: Iterable[Any]) -> list[tuple[str, frozenset[str]]]:
    # uv.lock is universal. Keep marker-gated entries so the snapshot covers every
    # supported platform, matching GitHub's native dependency-graph inventory.
    requests: list[tuple[str, frozenset[str]]] = []
    for entry in entries:
        if isinstance(entry, str):
            requests.append((normalize_package_name(entry), frozenset()))
        elif isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            extras = entry.get("extra", [])
            if not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras):
                raise ValueError(f"unsupported uv.lock dependency extras: {extras!r}")
            requests.append(
                (
                    normalize_package_name(entry["name"]),
                    frozenset(normalize_package_name(extra) for extra in extras),
                )
            )
        else:
            raise ValueError(f"unsupported uv.lock dependency entry: {entry!r}")
    return requests


def dependency_names(entries: Iterable[Any]) -> list[str]:
    return [name for name, _extras in dependency_requests(entries)]


def _find_root_package(packages: list[dict[str, Any]]) -> dict[str, Any]:
    roots = [package for package in packages if package.get("source", {}).get("editable") == "."]
    if len(roots) != 1:
        raise ValueError(f"expected one editable root package, found {len(roots)}")
    return roots[0]


def _registry_packages(packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for package in packages:
        source = package.get("source", {})
        if "registry" not in source:
            continue
        if source["registry"].rstrip("/") != PYPI_REGISTRY:
            raise ValueError(f"unsupported registry for {package.get('name')}: {source['registry']}")

        name = normalize_package_name(package["name"])
        if name in by_name:
            raise ValueError(f"multiple locked distributions normalize to {name!r}")
        by_name[name] = package
    return by_name


def _walk_dependencies(
    seeds: Iterable[tuple[str, frozenset[str]]],
    packages: Mapping[str, dict[str, Any]],
) -> dict[str, set[str]]:
    activated_extras: dict[str, set[str]] = {}
    pending = list(seeds)
    while pending:
        name, requested_extras = pending.pop()
        name = normalize_package_name(name)
        if name not in packages:
            continue

        first_visit = name not in activated_extras
        known_extras = activated_extras.setdefault(name, set())
        new_extras = set(requested_extras) - known_extras
        if not first_visit and not new_extras:
            continue

        package = packages[name]
        if first_visit:
            pending.extend(dependency_requests(package.get("dependencies", [])))
        optional_dependencies = package.get("optional-dependencies", {})
        for extra in new_extras:
            pending.extend(dependency_requests(optional_dependencies.get(extra, [])))
        known_extras.update(requested_extras)
    return activated_extras


def _activated_dependency_names(package: Mapping[str, Any], extras: Iterable[str]) -> set[str]:
    names = set(dependency_names(package.get("dependencies", [])))
    optional_dependencies = package.get("optional-dependencies", {})
    for extra in extras:
        names.update(dependency_names(optional_dependencies.get(extra, [])))
    return names


def build_manifest(lock_data: Mapping[str, Any], source_location: str = "uv.lock") -> dict[str, Any]:
    packages = list(lock_data.get("package", []))
    root = _find_root_package(packages)
    registry_packages = _registry_packages(packages)

    runtime_seeds = dependency_requests(root.get("dependencies", []))
    for dependencies in root.get("optional-dependencies", {}).values():
        runtime_seeds.extend(dependency_requests(dependencies))
    direct_runtime = {name for name, _extras in runtime_seeds}

    development_seeds: list[tuple[str, frozenset[str]]] = []
    for dependencies in root.get("dev-dependencies", {}).values():
        development_seeds.extend(dependency_requests(dependencies))
    direct_development = {name for name, _extras in development_seeds}

    runtime = _walk_dependencies(runtime_seeds, registry_packages)
    development = _walk_dependencies(development_seeds, registry_packages)
    included = set(runtime) | set(development)
    direct = direct_runtime | direct_development

    purls = {
        name: package_url(package["name"], str(package["version"]))
        for name, package in registry_packages.items()
        if name in included
    }
    resolved: dict[str, Any] = {}
    for name in sorted(included):
        package = registry_packages[name]
        purl = purls[name]
        extras = runtime.get(name, set()) | development.get(name, set())
        child_purls = sorted(
            purls[child]
            for child in _activated_dependency_names(package, extras)
            if child in purls
        )
        resolved[purl] = {
            "package_url": purl,
            "relationship": "direct" if name in direct else "indirect",
            "scope": "runtime" if name in runtime else "development",
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
