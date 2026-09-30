import base64
import json
from pathlib import Path
import tempfile
import unittest

from scripts.validate_t14_promotion import TARGETS, validate


def _proof(path: Path, target: str, source_sha: str, image_digest: str, number: str) -> None:
    statement = {
        "schema_version": 1,
        "statement_kind": "builder",
        "target_name": target,
        "application_revision": source_sha,
        "source_sha256": number * 64,
        "artifact_sha256": chr(ord(number) + 1) * 64,
        "image_digest": image_digest,
    }
    envelope = {
        "payloadType": "application/vnd.ninjasre.t14.builder.v1+json",
        "payload": base64.b64encode(json.dumps(statement, sort_keys=True).encode()).decode(),
        "signatures": [{"keyid": "t14-builder-test", "sig": "AA=="}],
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")


def _release(path: Path, digest: str) -> None:
    path.write_text(json.dumps({
        "component": "app",
        "image": "registry.lan.kyo.ninja/ninjasre/app",
        "digest": digest,
        "workload": "app",
        "container": "app",
    }), encoding="utf-8")


class T14PromotionTests(unittest.TestCase):
    def test_promotion_requires_distinct_baseline_and_proposed_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            proof_directory = tmp_path / "proofs"
            proof_directory.mkdir()
            baseline_sha = "a" * 40
            proposed_sha = "b" * 40
            baseline_digest = "sha256:" + "c" * 64
            proposed_digest = "sha256:" + "d" * 64
            _proof(proof_directory / "baseline.dsse", "baseline", baseline_sha, baseline_digest, "1")
            for target in TARGETS[1:]:
                _proof(
                    proof_directory / f"{target}.dsse",
                    target,
                    proposed_sha,
                    proposed_digest,
                    "2",
                )
            baseline_release = tmp_path / "baseline.json"
            proposed_release = tmp_path / "proposed.json"
            _release(baseline_release, baseline_digest)
            _release(proposed_release, proposed_digest)

            validate(
                proof_directory,
                baseline_release,
                proposed_release,
                baseline_sha,
                proposed_sha,
                "t14-builder-test",
            )


    def test_promotion_rejects_a_proposed_proof_signed_for_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            proof_directory = tmp_path / "proofs"
            proof_directory.mkdir()
            baseline_sha = "a" * 40
            proposed_sha = "b" * 40
            baseline_digest = "sha256:" + "c" * 64
            proposed_digest = "sha256:" + "d" * 64
            _proof(proof_directory / "baseline.dsse", "baseline", baseline_sha, baseline_digest, "1")
            for index, target in enumerate(TARGETS[1:], start=2):
                _proof(
                    proof_directory / f"{target}.dsse",
                    target,
                    baseline_sha,
                    baseline_digest,
                    str(index),
                )
            baseline_release = tmp_path / "baseline.json"
            proposed_release = tmp_path / "proposed.json"
            _release(baseline_release, baseline_digest)
            _release(proposed_release, proposed_digest)

            try:
                validate(
                    proof_directory,
                    baseline_release,
                    proposed_release,
                    baseline_sha,
                    proposed_sha,
                    "t14-builder-test",
                )
            except ValueError as error:
                self.assertIn("proposal proof does not match", str(error))
            else:
                self.fail("a proposal proof signed for baseline was accepted")


if __name__ == "__main__":
    unittest.main()
