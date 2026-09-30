#!/usr/bin/env python3
"""Validate a cross-repository T14 baseline artifact before image promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.validate_t14_promotion import _proof, _public_key
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from validate_t14_promotion import _proof, _public_key


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
MAX_OCI_BLOB_BYTES = 1 * 1024 * 1024 * 1024
MAX_OCI_JSON_BYTES = 4 * 1024 * 1024
RECEIPT_KEYS = {
    "schema_version",
    "target_name",
    "application_revision",
    "image_digest",
    "builder_proof_sha256",
    "oci_archive_sha256",
}
RELEASE_KEYS = {"component", "image", "digest", "workload", "container"}
ARCHIVE_NAME = "release-t14-baseline-app.oci.tar"
RECEIPT_NAME = "release-t14-baseline-app.json"
PROOF_NAME = "release-t14-baseline.builder.dsse"


@dataclass(frozen=True, slots=True)
class BaselineReceipt:
    """Validated public identity and hashes from a baseline artifact receipt."""

    application_revision: str
    image_digest: str
    builder_proof_sha256: str
    oci_archive_sha256: str


def _json_file(path: Path, label: str, maximum: int = MAX_OCI_JSON_BYTES) -> dict[str, Any]:
    """Return one bounded JSON object from a regular file."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    if path.stat().st_size > maximum:
        raise ValueError(f"{label} exceeds the configured size bound")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sha256_file(path: Path, label: str, maximum: int) -> str:
    """Return a bounded file hash with the digest prefix used by receipts."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    size = path.stat().st_size
    if size <= 0 or size > maximum:
        raise ValueError(f"{label} has an invalid size")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ValueError(f"{label} cannot be read") from error
    return "sha256:" + digest.hexdigest()


def _one_file(root: Path, name: str, label: str) -> Path:
    """Return the only regular artifact member with the requested basename."""
    matches = sorted(path for path in root.rglob(name) if path.is_file() and not path.is_symlink())
    if len(matches) != 1:
        raise ValueError(f"baseline artifact must contain exactly one {label}")
    return matches[0]


def _safe_extract(archive_path: Path, destination: Path) -> None:
    """Extract a bounded OCI tar archive without links or path traversal."""
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("OCI archive must be a regular file")
    size = archive_path.stat().st_size
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise ValueError("OCI archive has an invalid size")
    destination_root = destination.resolve()
    total_bytes = 0
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("OCI archive contains too many files")
            for member in members:
                if member.issym() or member.islnk() or not (member.isreg() or member.isdir()):
                    raise ValueError("OCI archive contains a non-regular member")
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or not member_path.parts
                    or any(part in {"", ".", ".."} for part in member_path.parts)
                ):
                    raise ValueError("OCI archive contains an unsafe path")
                if member.size < 0:
                    raise ValueError("OCI archive contains a negative file size")
                total_bytes += member.size
                if total_bytes > MAX_ARCHIVE_BYTES:
                    raise ValueError("OCI archive contents exceed the configured size bound")
                target = destination.joinpath(*member_path.parts)
                resolved = target.resolve()
                if resolved != destination_root and destination_root not in resolved.parents:
                    raise ValueError("OCI archive member escapes the extraction directory")
                if member.isdir():
                    if member.size != 0:
                        raise ValueError("OCI archive directory has a non-zero size")
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("OCI archive member cannot be read")
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                if target.stat().st_size != member.size:
                    raise ValueError("OCI archive member size changed while extracting")
    except tarfile.TarError as error:
        raise ValueError("OCI archive is not a readable tar archive") from error


def _descriptor(value: object, label: str) -> tuple[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an OCI descriptor")
    digest = value.get("digest")
    size = value.get("size")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError(f"{label} has an invalid digest")
    if type(size) is not int or size < 0:
        raise ValueError(f"{label} has an invalid size")
    return digest, size


def _blob(layout: Path, digest: str, size: int, label: str) -> bytes:
    if size > MAX_OCI_BLOB_BYTES:
        raise ValueError(f"{label} exceeds the configured size bound")
    path = layout / "blobs" / "sha256" / digest[7:]
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing from the OCI layout")
    content = path.read_bytes()
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest[7:]:
        raise ValueError(f"{label} digest or size does not match its content")
    return content


def _verify_oci_layout(archive_root: Path, expected_digest: str) -> None:
    """Verify the single manifest and all content referenced by a baseline OCI archive."""
    layout_record = _json_file(archive_root / "oci-layout", "OCI layout descriptor")
    if layout_record.get("imageLayoutVersion") != "1.0.0":
        raise ValueError("OCI layout version is unsupported")
    index = _json_file(archive_root / "index.json", "OCI image index")
    if index.get("schemaVersion") != 2:
        raise ValueError("OCI image index schema is unsupported")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError("baseline OCI layout must contain exactly one manifest")
    manifest_digest, manifest_size = _descriptor(manifests[0], "OCI image manifest descriptor")
    if manifest_digest != expected_digest:
        raise ValueError("OCI image digest does not match the baseline receipt")
    manifest_bytes = _blob(archive_root, manifest_digest, manifest_size, "OCI image manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("OCI image manifest is not valid JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ValueError("OCI image manifest schema is unsupported")
    if manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json":
        raise ValueError("OCI image manifest media type is unsupported")
    config_digest, config_size = _descriptor(manifest.get("config"), "OCI image config")
    _blob(archive_root, config_digest, config_size, "OCI image config")
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        raise ValueError("OCI image manifest layers are invalid")
    for index, layer in enumerate(layers):
        layer_digest, layer_size = _descriptor(layer, f"OCI image layer {index}")
        _blob(archive_root, layer_digest, layer_size, f"OCI image layer {index}")


def _receipt(path: Path, expected_source_sha: str) -> BaselineReceipt:
    data = _json_file(path, "baseline receipt")
    if set(data) != RECEIPT_KEYS:
        raise ValueError("baseline receipt has an unexpected schema")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("baseline receipt schema version is invalid")
    if data["target_name"] != "baseline":
        raise ValueError("baseline receipt target is invalid")
    source_sha = data["application_revision"]
    if not isinstance(source_sha, str) or GIT_SHA.fullmatch(source_sha) is None:
        raise ValueError("baseline receipt application revision is invalid")
    if source_sha != expected_source_sha:
        raise ValueError("baseline receipt does not match the declared source SHA")
    values = {}
    for key in ("image_digest", "builder_proof_sha256", "oci_archive_sha256"):
        value = data[key]
        if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
            raise ValueError(f"baseline receipt {key} is invalid")
        values[key] = value
    return BaselineReceipt(
        source_sha,
        values["image_digest"],
        values["builder_proof_sha256"],
        values["oci_archive_sha256"],
    )


def _release(path: Path) -> dict[str, str]:
    data = _json_file(path, "proposed release")
    if set(data) != RELEASE_KEYS or any(
        not isinstance(data[key], str) or not data[key] for key in RELEASE_KEYS
    ):
        raise ValueError("proposed release is invalid")
    if DIGEST.fullmatch(data["digest"]) is None:
        raise ValueError("proposed release digest is invalid")
    return {key: data[key] for key in RELEASE_KEYS}


def _artifact_file(directory: Path, name: str, label: str) -> Path:
    if not directory.is_dir():
        raise ValueError("baseline artifact directory does not exist")
    return _one_file(directory, name, label)


def validate(
    artifact_directory: Path,
    proposed_release_file: Path,
    expected_source_sha: str,
    builder_key_id: str,
    builder_public_key_file: Path,
    output_release_file: Path,
) -> BaselineReceipt:
    """Validate the historical artifact and write a registry-ready baseline fragment."""
    if GIT_SHA.fullmatch(expected_source_sha) is None:
        raise ValueError("baseline source revision must be a full Git SHA")
    archive = _artifact_file(artifact_directory, ARCHIVE_NAME, "OCI archive")
    receipt_file = _artifact_file(artifact_directory, RECEIPT_NAME, "receipt")
    proof = _artifact_file(artifact_directory, PROOF_NAME, "builder proof")
    receipt = _receipt(receipt_file, expected_source_sha)
    if _sha256_file(archive, "OCI archive", MAX_ARCHIVE_BYTES) != receipt.oci_archive_sha256:
        raise ValueError("OCI archive digest does not match the baseline receipt")
    if _sha256_file(proof, "builder proof", MAX_OCI_JSON_BYTES) != receipt.builder_proof_sha256:
        raise ValueError("builder proof digest does not match the baseline receipt")
    public_key = _public_key(builder_public_key_file)
    statement = _proof(proof, builder_key_id, public_key)
    if statement["target_name"] != "baseline":
        raise ValueError("baseline builder proof target is invalid")
    if statement["application_revision"] != receipt.application_revision:
        raise ValueError("baseline builder proof does not match the receipt source SHA")
    if statement["image_digest"] != receipt.image_digest:
        raise ValueError("baseline builder proof does not match the receipt image digest")
    if statement["artifact_sha256"] != receipt.image_digest[7:]:
        raise ValueError("baseline builder proof artifact does not match the OCI manifest digest")
    with tempfile.TemporaryDirectory(prefix="t14-baseline-oci-") as temporary:
        extracted = Path(temporary)
        _safe_extract(archive, extracted)
        layout = _one_file(extracted, "oci-layout", "OCI layout descriptor").parent
        index = _one_file(extracted, "index.json", "OCI image index")
        if index.parent != layout:
            raise ValueError("OCI layout descriptor and image index must share a root")
        _verify_oci_layout(layout, receipt.image_digest)
    proposed = _release(proposed_release_file)
    baseline = dict(proposed)
    baseline["digest"] = receipt.image_digest
    output_release_file.parent.mkdir(parents=True, exist_ok=True)
    output_release_file.write_text(json.dumps(baseline, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument("--proposed-release-file", type=Path, required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--builder-key-id", required=True)
    parser.add_argument("--builder-public-key-file", type=Path, required=True)
    parser.add_argument("--output-release-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = validate(
            args.artifact_directory,
            args.proposed_release_file,
            args.expected_source_sha,
            args.builder_key_id,
            args.builder_public_key_file,
            args.output_release_file,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid T14 baseline artifact: {error}", file=sys.stderr)
        return 2
    print(f"validated T14 baseline {receipt.application_revision} at {receipt.image_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
