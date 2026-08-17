#!/usr/bin/env python3
"""Update a strategic-merge images.yaml from trusted release fragments."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_release(directory: Path) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for path in sorted(directory.rglob("*.json")):
        if path.name.endswith(".spdx.json"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        required = {"component", "image", "digest", "workload", "container"}
        if not isinstance(data, dict) or set(data) != required or not DIGEST.fullmatch(str(data.get("digest", ""))):
            raise ValueError(f"invalid release fragment: {path}")
        target = (data["workload"], data["container"])
        if target in result:
            raise ValueError(f"duplicate release target: {target[0]}/{target[1]}")
        result[target] = f"{data['image']}@{data['digest']}"
    if not result:
        raise ValueError("release directory contains no JSON fragments")
    return result


def update_chart_values(path: Path, release: dict[tuple[str, str], str]) -> None:
    """Apply immutable digests to a wrapper-chart values file.

    Each top-level alias section whose ``fullnameOverride`` equals the
    fragment workload receives ``image.repository`` and ``image.digest``.
    """
    by_workload: dict[str, tuple[str, str]] = {}
    for (workload, _container), target in release.items():
        if workload in by_workload:
            raise ValueError(f"chart-values mode requires one container per workload: {workload}")
        image, _, digest = target.rpartition("@")
        by_workload[workload] = (image, digest)

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"chart values file must be a mapping: {path}")
    remaining = set(by_workload)
    for section, values in document.items():
        if not isinstance(values, dict):
            continue
        workload = values.get("fullnameOverride")
        if workload not in remaining:
            continue
        image, digest = by_workload[workload]
        image_values = values.setdefault("image", {})
        if not isinstance(image_values, dict):
            raise ValueError(f"{path}: section {section} has a non-mapping image")
        image_values["repository"] = image
        image_values["digest"] = digest
        remaining.remove(workload)
    if remaining:
        missing = ", ".join(sorted(remaining))
        raise ValueError(f"chart values is missing release targets (fullnameOverride): {missing}")
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def update(path: Path, release: dict[tuple[str, str], str]) -> None:
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    remaining = set(release)
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        workload = document.get("metadata", {}).get("name")
        containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            target = (workload, container.get("name"))
            if target in release:
                container["image"] = release[target]
                remaining.remove(target)
    if remaining:
        missing = ", ".join(f"{workload}/{container}" for workload, container in sorted(remaining))
        raise ValueError(f"images.yaml is missing release targets: {missing}")
    rendered = "---\n".join(
        yaml.safe_dump(document, sort_keys=False, explicit_start=False).rstrip() + "\n"
        for document in documents if document is not None
    )
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("images_file", type=Path)
    parser.add_argument("release_directory", type=Path)
    parser.add_argument("--mode", choices=("kustomize", "chart-values"), default="kustomize")
    args = parser.parse_args()
    apply = update_chart_values if args.mode == "chart-values" else update
    try:
        apply(args.images_file, load_release(args.release_directory))
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"cannot update GitOps images: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

