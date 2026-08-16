#!/usr/bin/env python3
"""Validate and render the deliberately small application delivery contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
APP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
IMAGE = re.compile(r"^registry\.lan\.kyo\.ninja/[a-z0-9._/-]+$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
TARGET = re.compile(r"^[A-Za-z0-9_.-]+$")
PLATFORMS = {"linux/amd64", "linux/arm64"}
ROLLOUT_PROFILES = {"deployment", "bluegreen", "canary"}
ROOT_KEYS = {"schemaVersion", "application", "components", "gitops"}
COMPONENT_KEYS = {
    "name", "image", "context", "dockerfile", "workload", "container", "target",
    "platforms", "rolloutProfile"
}
GITOPS_KEYS = {
    "repository", "baseBranch", "stagingBranch", "productionBranch", "stagingPath",
    "productionPath", "stagingApplication", "productionApplication"
}


class InvalidDescriptor(ValueError):
    pass


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidDescriptor(f"{label} must be a mapping")
    return value


def _string(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidDescriptor(f"{label} has an invalid value")
    return value


def _keys(value: dict[str, Any], expected: set[str], required: set[str], label: str) -> None:
    unknown = set(value) - expected
    missing = required - set(value)
    if unknown:
        raise InvalidDescriptor(f"{label} contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise InvalidDescriptor(f"{label} is missing keys: {', '.join(sorted(missing))}")


def _relative_path(value: Any, label: str, *, must_exist: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or "\\" in value:
        raise InvalidDescriptor(f"{label} must be a safe relative POSIX path")
    if value == ".":
        return value
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise InvalidDescriptor(f"{label} must not be absolute or contain '.'/'..'")
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts):
        raise InvalidDescriptor(f"{label} contains unsupported characters")
    if must_exist and not Path(value).exists():
        raise InvalidDescriptor(f"{label} does not exist: {value}")
    return value


def load_and_validate(path: Path, *, check_files: bool = True) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidDescriptor(f"cannot load {path}: {exc}") from exc
    data = _mapping(raw, "descriptor")
    _keys(data, ROOT_KEYS, ROOT_KEYS, "descriptor")
    if data["schemaVersion"] != 1:
        raise InvalidDescriptor("schemaVersion must be 1")
    _string(data["application"], "application", APP_NAME)

    components = data["components"]
    if not isinstance(components, list) or not 1 <= len(components) <= 20:
        raise InvalidDescriptor("components must contain between 1 and 20 entries")
    names: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for index, raw_component in enumerate(components):
        label = f"components[{index}]"
        component = _mapping(raw_component, label)
        _keys(
            component, COMPONENT_KEYS,
            {"name", "image", "context", "dockerfile", "workload", "container", "rolloutProfile"},
            label,
        )
        name = _string(component["name"], f"{label}.name", NAME)
        _string(component["image"], f"{label}.image", IMAGE)
        workload = _string(component["workload"], f"{label}.workload", NAME)
        container = _string(component["container"], f"{label}.container", NAME)
        if name in names or (workload, container) in targets:
            raise InvalidDescriptor(f"{label} duplicates a component name or workload/container target")
        names.add(name)
        targets.add((workload, container))
        _relative_path(component["context"], f"{label}.context", must_exist=check_files)
        _relative_path(component["dockerfile"], f"{label}.dockerfile", must_exist=check_files)
        if "target" in component:
            _string(component["target"], f"{label}.target", TARGET)
        platforms = component.get("platforms", ["linux/amd64"])
        if not isinstance(platforms, list) or not platforms or len(platforms) != len(set(platforms)):
            raise InvalidDescriptor(f"{label}.platforms must be a non-empty unique list")
        if not set(platforms) <= PLATFORMS:
            raise InvalidDescriptor(f"{label}.platforms contains an unsupported platform")
        if component["rolloutProfile"] not in ROLLOUT_PROFILES:
            raise InvalidDescriptor(f"{label}.rolloutProfile is invalid")

    gitops = _mapping(data["gitops"], "gitops")
    _keys(gitops, GITOPS_KEYS, GITOPS_KEYS, "gitops")
    _string(gitops["repository"], "gitops.repository", REPOSITORY)
    for key in ("baseBranch", "stagingBranch", "productionBranch"):
        _string(gitops[key], f"gitops.{key}", REF)
    for key in ("stagingPath", "productionPath"):
        _relative_path(gitops[key], f"gitops.{key}")
    for key in ("stagingApplication", "productionApplication"):
        _string(gitops[key], f"gitops.{key}", APP_NAME)
    return data


def matrix(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    include = []
    for item in data["components"]:
        include.append({
            "name": item["name"],
            "image": item["image"],
            "context": item["context"],
            "dockerfile": item["dockerfile"],
            "workload": item["workload"],
            "container": item["container"],
            "target": item.get("target", ""),
            "platforms": ",".join(item.get("platforms", ["linux/amd64"])),
            "rollout_profile": item["rolloutProfile"],
        })
    return {"include": include}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("descriptor", type=Path)
    parser.add_argument("--no-check-files", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--print-matrix", action="store_true")
    args = parser.parse_args()
    try:
        data = load_and_validate(args.descriptor, check_files=not args.no_check_files)
    except InvalidDescriptor as exc:
        print(f"invalid descriptor: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(matrix(data), separators=(",", ":"))
    if args.github_output:
        gitops = data["gitops"]
        values = {
            "application": data["application"], "matrix": rendered,
            "gitops_repository": gitops["repository"], "gitops_base_branch": gitops["baseBranch"],
            "gitops_staging_branch": gitops["stagingBranch"],
            "gitops_production_branch": gitops["productionBranch"],
            "gitops_staging_path": gitops["stagingPath"],
            "gitops_production_path": gitops["productionPath"],
            "gitops_staging_application": gitops["stagingApplication"],
            "gitops_production_application": gitops["productionApplication"],
            "gitops_repository_name": gitops["repository"].split("/", 1)[1],
        }
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                print(f"{key}={value}", file=output)
    if args.print_matrix:
        print(rendered)
    else:
        print(f"valid descriptor for {data['application']} ({len(data['components'])} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
