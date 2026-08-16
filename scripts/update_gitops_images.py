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
    args = parser.parse_args()
    try:
        update(args.images_file, load_release(args.release_directory))
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"cannot update GitOps images: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

