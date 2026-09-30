#!/usr/bin/env python3
"""Validate that a T14 promotion carries separate baseline and proposal proofs."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


TARGETS = (
    "baseline",
    "proposed",
    "proposed-without-relations",
    "proposed-without-t06_readers",
    "proposed-without-t07_probes",
    "proposed-without-t09_planning",
)
PAYLOAD_TYPE = "application/vnd.ninjasre.t14.builder.v1+json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """Return the DSSE pre-authentication encoding for one payload."""
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def _public_key(path: Path) -> Ed25519PublicKey:
    """Load one pinned Ed25519 public key without exposing its bytes."""
    raw = path.read_bytes()
    if len(raw) == 32:
        return Ed25519PublicKey.from_public_bytes(raw)
    loaded = serialization.load_pem_public_key(raw)
    if not isinstance(loaded, Ed25519PublicKey):
        raise ValueError("builder public key is not Ed25519")
    return loaded


def _release(path: Path) -> str:
    """Return the immutable image reference from one release fragment."""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"component", "image", "digest", "workload", "container"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"invalid release fragment: {path}")
    digest = value.get("digest")
    image = value.get("image")
    if not isinstance(image, str) or not image or not isinstance(digest, str):
        raise ValueError(f"invalid release fragment: {path}")
    if IMAGE_DIGEST.fullmatch(digest) is None:
        raise ValueError(f"invalid release digest: {path}")
    return f"{image}@{digest}"


def _json(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _proof(path: Path, builder_key_id: str, public_key: Ed25519PublicKey) -> dict[str, Any]:
    """Decode and structurally validate one builder DSSE envelope."""
    raw = path.read_bytes()
    if not raw or len(raw) > 65_536:
        raise ValueError(f"invalid builder proof size: {path}")
    try:
        envelope = _json(json.loads(raw), f"builder envelope {path}")
        payload_type = envelope["payloadType"]
        payload = envelope["payload"]
        signatures = envelope["signatures"]
        if payload_type != PAYLOAD_TYPE or not isinstance(payload, str):
            raise ValueError(f"builder envelope has an unexpected payload type: {path}")
        if not isinstance(signatures, list) or len(signatures) != 1:
            raise ValueError(f"builder envelope must have one signature: {path}")
        signature = _json(signatures[0], f"builder signature {path}")
        if signature.get("keyid") != builder_key_id:
            raise ValueError(f"builder envelope key id does not match descriptor: {path}")
        for key in ("sig",):
            encoded = signature.get(key)
            if not isinstance(encoded, str) or not encoded:
                raise ValueError(f"builder signature is missing {key}: {path}")
            base64.b64decode(encoded, validate=True)
        payload_bytes = base64.b64decode(payload, validate=True)
        signature_bytes = base64.b64decode(signature["sig"], validate=True)
        try:
            public_key.verify(signature_bytes, _dsse_pae(PAYLOAD_TYPE, payload_bytes))
        except InvalidSignature as error:
            raise ValueError(f"builder signature verification failed: {path}") from error
        statement = _json(json.loads(payload_bytes), f"builder statement {path}")
    except (KeyError, OSError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
        if isinstance(error, ValueError) and str(error).startswith(("builder envelope", "builder signature", "builder statement")):
            raise
        raise ValueError(f"invalid builder proof: {path}") from error
    required = {
        "schema_version",
        "statement_kind",
        "target_name",
        "application_revision",
        "source_sha256",
        "artifact_sha256",
        "image_digest",
    }
    if not required <= set(statement):
        raise ValueError(f"builder statement is incomplete: {path}")
    if statement.get("schema_version") != 1 or statement["statement_kind"] != "builder":
        raise ValueError(f"builder statement kind is invalid: {path}")
    if not isinstance(statement["application_revision"], str) or GIT_SHA.fullmatch(statement["application_revision"]) is None:
        raise ValueError(f"builder application revision is invalid: {path}")
    for key in ("source_sha256", "artifact_sha256"):
        value = statement[key]
        if not isinstance(value, str) or HEX_DIGEST.fullmatch(value) is None:
            raise ValueError(f"builder {key} is invalid: {path}")
    image_digest = statement["image_digest"]
    if not isinstance(image_digest, str) or IMAGE_DIGEST.fullmatch(image_digest) is None:
        raise ValueError(f"builder image digest is invalid: {path}")
    return statement


def validate(
    proof_directory: Path,
    baseline_release: Path,
    proposed_release: Path,
    baseline_source_sha: str,
    proposed_source_sha: str,
    builder_key_id: str,
    builder_public_key_file: Path,
) -> None:
    """Raise when six proofs do not bind distinct baseline and proposal artifacts."""
    if GIT_SHA.fullmatch(baseline_source_sha) is None or GIT_SHA.fullmatch(proposed_source_sha) is None:
        raise ValueError("baseline and proposed source revisions must be full Git SHAs")
    if baseline_source_sha == proposed_source_sha:
        raise ValueError("baseline and proposed source revisions must be distinct")
    files = sorted(proof_directory.glob("*.dsse"))
    if len(files) != len(TARGETS):
        raise ValueError("T14 promotion requires exactly six builder proofs")
    try:
        public_key = _public_key(builder_public_key_file)
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("builder public key cannot be loaded") from error
    statements = [_proof(path, builder_key_id, public_key) for path in files]
    by_target: dict[str, dict[str, Any]] = {}
    for statement in statements:
        target = statement["target_name"]
        if target not in TARGETS or target in by_target:
            raise ValueError("T14 builder proofs must cover each target exactly once")
        by_target[target] = statement
    if set(by_target) != set(TARGETS):
        raise ValueError("T14 builder proofs must cover all six declared targets")
    baseline = by_target["baseline"]
    proposed = [by_target[target] for target in TARGETS if target != "baseline"]
    baseline_image = _release(baseline_release)
    proposed_image = _release(proposed_release)
    if baseline["application_revision"] != baseline_source_sha:
        raise ValueError("baseline proof does not match the declared historical source SHA")
    if any(statement["application_revision"] != proposed_source_sha for statement in proposed):
        raise ValueError("proposal proof does not match the current source SHA")
    if baseline["image_digest"] != baseline_image.rsplit("@", 1)[1]:
        raise ValueError("baseline proof does not match the baseline release digest")
    if any(statement["image_digest"] != proposed_image.rsplit("@", 1)[1] for statement in proposed):
        raise ValueError("proposal proof does not match the proposed release digest")
    for field in ("application_revision", "source_sha256", "artifact_sha256", "image_digest"):
        if any(statement[field] == baseline[field] for statement in proposed):
            raise ValueError(f"baseline and proposal share {field}")
    for field in ("source_sha256", "artifact_sha256", "image_digest"):
        if len({statement[field] for statement in proposed}) != 1:
            raise ValueError(f"proposal proofs disagree on {field}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-directory", type=Path, required=True)
    parser.add_argument("--baseline-release-file", type=Path, required=True)
    parser.add_argument("--proposed-release-file", type=Path, required=True)
    parser.add_argument("--baseline-source-sha", required=True)
    parser.add_argument("--proposed-source-sha", required=True)
    parser.add_argument("--builder-key-id", required=True)
    parser.add_argument("--builder-public-key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate(
            args.proof_directory,
            args.baseline_release_file,
            args.proposed_release_file,
            args.baseline_source_sha,
            args.proposed_source_sha,
            args.builder_key_id,
            args.builder_public_key_file,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid T14 promotion: {error}", file=sys.stderr)
        return 2
    print("T14 promotion carries distinct baseline and proposal proofs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
