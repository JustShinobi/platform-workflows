#!/usr/bin/env python3
"""Update a strategic-merge images.yaml from trusted release fragments."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    """Validated image metadata emitted by one build matrix entry."""

    component: str
    image: str
    digest: str
    workload: str
    container: str

    @property
    def target(self) -> tuple[str, str]:
        """Return the GitOps workload/container identity for this image."""
        return self.workload, self.container

    @property
    def image_ref(self) -> str:
        """Return the immutable registry reference for this image."""
        return f"{self.image}@{self.digest}"


def _load_release_records(directory: Path) -> dict[str, ReleaseRecord]:
    records: dict[str, ReleaseRecord] = {}
    targets: set[tuple[str, str]] = set()
    for path in sorted(directory.rglob("*.json")):
        if path.name.endswith(".spdx.json"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        required = {"component", "image", "digest", "workload", "container"}
        if (
            not isinstance(data, dict)
            or set(data) != required
            or not all(isinstance(data.get(key), str) for key in required)
            or not DIGEST.fullmatch(str(data.get("digest", "")))
        ):
            raise ValueError(f"invalid release fragment: {path}")
        record = ReleaseRecord(
            component=data["component"],
            image=data["image"],
            digest=data["digest"],
            workload=data["workload"],
            container=data["container"],
        )
        if record.component in records:
            raise ValueError(f"duplicate release component: {record.component}")
        if record.target in targets:
            raise ValueError(
                f"duplicate release target: {record.target[0]}/{record.target[1]}"
            )
        records[record.component] = record
        targets.add(record.target)
    if not records:
        raise ValueError("release directory contains no JSON fragments")
    return records


def load_release(directory: Path) -> dict[tuple[str, str], str]:
    """Return immutable image references indexed by workload and container."""
    result = {
        record.target: record.image_ref
        for record in _load_release_records(directory).values()
    }
    return result


def load_release_records(directory: Path) -> dict[str, ReleaseRecord]:
    """Return validated release records indexed by descriptor component name."""
    return _load_release_records(directory)


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


def update_chart_values(
    path: Path,
    release: dict[tuple[str, str], str],
    allow_missing_targets: set[tuple[str, str]] | None = None,
) -> None:
    """Apply immutable digests to a wrapper-chart values file.

    Each top-level alias section whose ``fullnameOverride`` equals the
    fragment workload receives ``image.repository`` and ``image.digest``.
    """
    by_workload: dict[str, tuple[str, str]] = {}
    for (workload, _container), target in release.items():
        if workload in by_workload:
            raise ValueError(
                f"chart-values mode requires one container per workload: {workload}"
            )
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
    remaining -= {workload for workload, _container in (allow_missing_targets or set())}
    if remaining:
        missing = ", ".join(sorted(remaining))
        raise ValueError(
            f"chart values is missing release targets (fullnameOverride): {missing}"
        )
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def update(
    path: Path,
    release: dict[tuple[str, str], str],
    allow_missing_targets: set[tuple[str, str]] | None = None,
) -> None:
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    remaining = set(release)
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        workload = document.get("metadata", {}).get("name")
        containers = (
            document.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        for container in containers:
            target = (workload, container.get("name"))
            if target in release:
                container["image"] = release[target]
                remaining.remove(target)
    remaining -= allow_missing_targets or set()
    if remaining:
        missing = ", ".join(
            f"{workload}/{container}" for workload, container in sorted(remaining)
        )
        raise ValueError(f"images.yaml is missing release targets: {missing}")
    rendered = "---\n".join(
        yaml.safe_dump(document, sort_keys=False, explicit_start=False).rstrip() + "\n"
        for document in documents
        if document is not None
    )
    path.write_text(rendered, encoding="utf-8")


def update_t14(
    path: Path,
    release: dict[tuple[str, str], str],
    targets: list[str],
    component: str,
    shared_components: list[str] | None = None,
    baseline_release: dict[tuple[str, str], str] | None = None,
    release_records: dict[str, ReleaseRecord] | None = None,
) -> None:
    """Apply all T14 image references, including Job initContainers."""
    component_record = (release_records or {}).get(component)
    component_target = (
        component_record.target if component_record else (component, component)
    )
    proposed_image = (
        component_record.image_ref
        if component_record
        else release.get(component_target)
    )
    if proposed_image is None:
        raise ValueError(
            f"release directory is missing T14 image component: "
            f"{component_target[0]}/{component_target[1]}"
        )
    expected = set(targets)
    if len(expected) != len(targets) or not expected:
        raise ValueError("T14 targets must be a non-empty unique list")
    if "baseline" in expected:
        if baseline_release is None:
            raise ValueError("T14 promotion requires a separate baseline release")
        baseline_image = baseline_release.get(component_target)
        if baseline_image is None:
            raise ValueError(
                f"baseline release is missing T14 image component: "
                f"{component_target[0]}/{component_target[1]}"
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
        matching = [
            container
            for container in containers
            if container.get("name") == component_target[1]
        ]
        if len(matching) != 1:
            raise ValueError(
                f"T14 target {target} must contain exactly one {component_target[1]} container"
            )
        matching[0]["image"] = (
            baseline_image if target == "baseline" else proposed_image
        )
        found.add(target)
    missing = expected - found
    if missing:
        raise ValueError(
            f"images.yaml is missing T14 targets: {', '.join(sorted(missing))}"
        )

    # The auditor executes the proposed app image from both its initContainer
    # and its main container. They are deliberately updated together so the
    # ten GitOps image references cannot split across two application builds.
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Job":
            continue
        if document.get("metadata", {}).get("name") != "t14-judge-auditor":
            continue
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        judge_containers = [
            *pod_spec.get("initContainers", []),
            *pod_spec.get("containers", []),
        ]
        judge_names = {"stage-t14-judge-key", "auditor"}
        matching = [
            container
            for container in judge_containers
            if container.get("name") in judge_names
        ]
        if {container.get("name") for container in matching} != judge_names:
            raise ValueError(
                "t14-judge-auditor must contain stage-t14-judge-key and auditor containers"
            )
        for container in matching:
            container["image"] = proposed_image

    for shared_component in shared_components or []:
        shared_record = (release_records or {}).get(shared_component)
        shared_target = (
            shared_record.target
            if shared_record
            else (shared_component, shared_component)
        )
        shared_image = (
            shared_record.image_ref if shared_record else release.get(shared_target)
        )
        if shared_image is None:
            raise ValueError(
                f"release directory is missing T14 shared image: "
                f"{shared_target[0]}/{shared_target[1]}"
            )
        shared_found = 0
        for document in documents:
            if not isinstance(document, dict) or document.get("kind") not in {
                "Deployment",
                "Job",
            }:
                continue
            template = document.get("spec", {}).get("template", {})
            labels = template.get("metadata", {}).get("labels", {})
            pod_spec = template.get("spec", {})
            containers = [
                *pod_spec.get("initContainers", []),
                *pod_spec.get("containers", []),
            ]
            is_shared_deployment = labels.get("ninjasre.io/t14-role") == "shared"
            workload = document.get("metadata", {}).get("name")
            if is_shared_deployment:
                matching = [
                    container
                    for container in containers
                    if container.get("name") in {shared_component, shared_target[1]}
                ]
                if not matching:
                    continue
            elif workload == shared_target[0]:
                matching = [
                    container
                    for container in containers
                    if container.get("name") == shared_target[1]
                ]
            else:
                continue
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
        for document in documents
        if document is not None
    )
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("images_file", type=Path)
    parser.add_argument("release_directory", type=Path)
    parser.add_argument(
        "--mode", choices=("kustomize", "chart-values"), default="kustomize"
    )
    parser.add_argument("--t14-targets", nargs="+", default=[])
    parser.add_argument("--t14-component", default="app")
    parser.add_argument("--t14-shared-components", nargs="+", default=[])
    parser.add_argument("--t14-baseline-release-file", type=Path)
    parser.add_argument("--allow-missing-components", nargs="*", default=[])
    args = parser.parse_args()
    try:
        records = load_release_records(args.release_directory)
        release = {record.target: record.image_ref for record in records.values()}
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
                records,
            )
        else:
            apply = update_chart_values if args.mode == "chart-values" else update
            allow_missing = {
                records[name].target
                for name in args.allow_missing_components
                if name in records
            }
            unknown = set(args.allow_missing_components) - set(records)
            if unknown:
                raise ValueError(
                    f"release directory is missing allow-listed components: {', '.join(sorted(unknown))}"
                )
            apply(args.images_file, release, allow_missing)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"cannot update GitOps images: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
