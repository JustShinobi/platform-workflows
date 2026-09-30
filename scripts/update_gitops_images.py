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


def load_release_fragment(path: Path) -> dict[tuple[str, str], str]:
    """Return one immutable release fragment for a variant-specific promotion."""
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"component", "image", "digest", "workload", "container"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError(f"invalid release fragment: {path}")
    digest = str(data.get("digest", ""))
    if not DIGEST.fullmatch(digest):
        raise ValueError(f"invalid release fragment: {path}")
    target = (data["workload"], data["container"])
    return {target: f"{data['image']}@{digest}"}


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


def update_t14(
    path: Path,
    release: dict[tuple[str, str], str],
    targets: list[str],
    component: str,
    shared_components: list[str] | None = None,
    baseline_release: dict[tuple[str, str], str] | None = None,
) -> None:
    """Apply distinct baseline and proposed digests to the declared T14 arms."""
    proposed_image = release.get((component, component))
    if proposed_image is None:
        raise ValueError(f"release directory is missing T14 image component: {component}/{component}")
    expected = set(targets)
    if len(expected) != len(targets) or not expected:
        raise ValueError("T14 targets must be a non-empty unique list")
    if "baseline" in expected:
        if baseline_release is None:
            raise ValueError("T14 promotion requires a separate baseline release")
        baseline_image = baseline_release.get((component, component))
        if baseline_image is None:
            raise ValueError(
                f"baseline release is missing T14 image component: {component}/{component}"
            )
        if baseline_image == proposed_image:
            raise ValueError("T14 baseline and proposed image digests must be distinct")
    else:
        baseline_image = None
    found: set[str] = set()
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        template = document.get("spec", {}).get("template", {})
        labels = template.get("metadata", {}).get("labels", {})
        target = labels.get("ninjasre.io/t14-arm")
        if target is None:
            continue
        if target not in expected:
            raise ValueError(f"images.yaml contains an undeclared T14 target: {target}")
        if target in found:
            raise ValueError(f"images.yaml contains duplicate T14 target: {target}")
        containers = template.get("spec", {}).get("containers", [])
        matching = [container for container in containers if container.get("name") == component]
        if len(matching) != 1:
            raise ValueError(f"T14 target {target} must contain exactly one {component} container")
        matching[0]["image"] = baseline_image if target == "baseline" else proposed_image
        found.add(target)
    missing = expected - found
    if missing:
        raise ValueError(f"images.yaml is missing T14 targets: {', '.join(sorted(missing))}")
    for shared_component in shared_components or []:
        shared_image = release.get((shared_component, shared_component))
        if shared_image is None:
            raise ValueError(
                f"release directory is missing T14 shared image: "
                f"{shared_component}/{shared_component}"
            )
        shared_found = 0
        for document in documents:
            if not isinstance(document, dict) or document.get("kind") != "Deployment":
                continue
            labels = document.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
            if labels.get("ninjasre.io/t14-role") != "shared":
                continue
            containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            matching = [container for container in containers if container.get("name") == shared_component]
            if len(matching) != 1:
                raise ValueError(
                    f"shared T14 workload must contain exactly one {shared_component} container"
                )
            matching[0]["image"] = shared_image
            shared_found += 1
        if shared_found != 1:
            raise ValueError(
                f"images.yaml must contain exactly one shared T14 {shared_component} workload"
            )
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
    parser.add_argument("--t14-targets", nargs="+", default=[])
    parser.add_argument("--t14-component", default="app")
    parser.add_argument("--t14-shared-components", nargs="+", default=[])
    parser.add_argument("--t14-baseline-release-file", type=Path)
    args = parser.parse_args()
    try:
        release = load_release(args.release_directory)
        if args.t14_targets:
            baseline_release = None
            if args.t14_baseline_release_file is not None:
                baseline_release = load_release_fragment(args.t14_baseline_release_file)
            update_t14(
                args.images_file,
                release,
                args.t14_targets,
                args.t14_component,
                args.t14_shared_components,
                baseline_release,
            )
        else:
            apply = update_chart_values if args.mode == "chart-values" else update
            apply(args.images_file, release)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"cannot update GitOps images: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
