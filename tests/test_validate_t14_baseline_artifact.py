import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.validate_t14_baseline_artifact import validate
from scripts.validate_t14_promotion import PAYLOAD_TYPE, _dsse_pae


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _oci_archive(path: Path) -> str:
    config = b"{}"
    layer = b"layer"
    config_digest = _digest(config)
    layer_digest = _digest(layer)
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": config_digest, "size": len(config)},
            "layers": [{"digest": layer_digest, "size": len(layer)}],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    image_digest = _digest(manifest)
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [{"digest": image_digest, "size": len(manifest)}],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    layout = b'{"imageLayoutVersion":"1.0.0"}\n'
    blobs = {
        f"blobs/sha256/{config_digest[7:]}": config,
        f"blobs/sha256/{layer_digest[7:]}": layer,
        f"blobs/sha256/{image_digest[7:]}": manifest,
    }
    with tarfile.open(path, "w") as archive:
        for name in ("blobs", "blobs/sha256"):
            info = tarfile.TarInfo(name + "/")
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, content in {
            "oci-layout": layout,
            "index.json": index,
            **blobs,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return image_digest


def _proof(path: Path, source_sha: str, image_digest: str, signer: Ed25519PrivateKey) -> None:
    statement = {
        "schema_version": 1,
        "statement_kind": "builder",
        "target_name": "baseline",
        "application_revision": source_sha,
        "source_sha256": "1" * 64,
        "artifact_sha256": image_digest[7:],
        "image_digest": image_digest,
    }
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    envelope = {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [{
            "keyid": "t14-builder-test",
            "sig": base64.b64encode(signer.sign(_dsse_pae(PAYLOAD_TYPE, payload))).decode(),
        }],
    }
    path.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")


def _receipt(
    directory: Path, source_sha: str, image_digest: str, signer: Ed25519PrivateKey
) -> None:
    archive = directory / "release-t14-baseline-app.oci.tar"
    proof = directory / "release-t14-baseline.builder.dsse"
    _proof(proof, source_sha, image_digest, signer)
    receipt = {
        "schema_version": 1,
        "target_name": "baseline",
        "application_revision": source_sha,
        "image_digest": image_digest,
        "builder_proof_sha256": _digest(proof.read_bytes()),
        "oci_archive_sha256": _digest(archive.read_bytes()),
    }
    (directory / "release-t14-baseline-app.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class T14BaselineArtifactTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[str, str, Path, Path]:
        source_sha = "a" * 40
        signer = Ed25519PrivateKey.generate()
        public_key = root / "builder-public-key"
        public_key.write_bytes(signer.public_key().public_bytes_raw())
        image_digest = _oci_archive(root / "release-t14-baseline-app.oci.tar")
        _receipt(root, source_sha, image_digest, signer)
        proposed = root / "proposed.json"
        proposed.write_text(
            json.dumps({
                "component": "app",
                "image": "registry.lan.kyo.ninja/ninjasre/app",
                "digest": "sha256:" + "d" * 64,
                "workload": "app",
                "container": "app",
            }),
            encoding="utf-8",
        )
        return source_sha, image_digest, public_key, proposed

    def test_validates_receipt_oci_and_dsse_and_writes_distinct_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_sha, image_digest, public_key, proposed = self._fixture(root)
            output = root / "baseline-release.json"

            receipt = validate(root, proposed, source_sha, "t14-builder-test", public_key, output)

            self.assertEqual(receipt.image_digest, image_digest)
            self.assertEqual(json.loads(output.read_text())["digest"], image_digest)
            self.assertEqual(
                json.loads(output.read_text())["image"],
                "registry.lan.kyo.ninja/ninjasre/app",
            )
            self.assertNotEqual(json.loads(output.read_text())["digest"], "sha256:" + "d" * 64)

    def test_rejects_an_archive_tampered_after_the_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_sha, _, public_key, proposed = self._fixture(root)
            archive = root / "release-t14-baseline-app.oci.tar"
            archive.write_bytes(archive.read_bytes() + b"tampered")

            with self.assertRaisesRegex(ValueError, "OCI archive digest"):
                validate(
                    root, proposed, source_sha, "t14-builder-test", public_key, root / "out.json"
                )

    def test_rejects_a_tampered_builder_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_sha, _, public_key, proposed = self._fixture(root)
            proof = root / "release-t14-baseline.builder.dsse"
            envelope = json.loads(proof.read_text())
            envelope["signatures"][0]["sig"] = base64.b64encode(b"invalid").decode()
            proof.write_text(json.dumps(envelope), encoding="utf-8")
            receipt = root / "release-t14-baseline-app.json"
            receipt_data = json.loads(receipt.read_text())
            receipt_data["builder_proof_sha256"] = _digest(proof.read_bytes())
            receipt.write_text(json.dumps(receipt_data), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "signature verification failed"):
                validate(
                    root, proposed, source_sha, "t14-builder-test", public_key, root / "out.json"
                )


if __name__ == "__main__":
    unittest.main()
