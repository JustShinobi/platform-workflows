import json
from pathlib import Path
import tempfile
import unittest

import yaml

from scripts.update_gitops_images import load_release, update, update_chart_values


class GitOpsImageTests(unittest.TestCase):
    def test_updates_exact_workload_and_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text(
                "kind: Deployment\nmetadata:\n  name: app-api\nspec:\n  template:\n    spec:\n"
                "      containers:\n        - name: api\n          image: old@sha256:"
                + "0" * 64 + "\n",
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "sbom-api.spdx.json").write_text(json.dumps({"spdxVersion": "SPDX-2.3"}), encoding="utf-8")
            (release_dir / "api.json").write_text(json.dumps({
                "component": "api", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app-api", "container": "api"
            }), encoding="utf-8")
            update(images, load_release(release_dir))
            data = yaml.safe_load(images.read_text(encoding="utf-8"))
            self.assertEqual(
                data["spec"]["template"]["spec"]["containers"][0]["image"],
                "registry.lan/app@sha256:" + "a" * 64,
            )

    def test_chart_values_updates_alias_by_fullname_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = root / "values-prd.yaml"
            values.write_text(
                "api:\n  fullnameOverride: app-api\n  image:\n    repository: registry.lan/app\n"
                "    tag: latest\nworker:\n  fullnameOverride: app-worker\n  image:\n"
                "    repository: registry.lan/app\n    tag: latest\n",
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "api.json").write_text(json.dumps({
                "component": "api", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app-api", "container": "api"
            }), encoding="utf-8")
            update_chart_values(values, load_release(release_dir))
            data = yaml.safe_load(values.read_text(encoding="utf-8"))
            self.assertEqual(data["api"]["image"]["repository"], "registry.lan/app")
            self.assertEqual(data["api"]["image"]["digest"], "sha256:" + "a" * 64)
            self.assertNotIn("digest", data["worker"]["image"])

    def test_chart_values_fails_closed_for_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = root / "values-prd.yaml"
            values.write_text("api:\n  fullnameOverride: other\n", encoding="utf-8")
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "api.json").write_text(json.dumps({
                "component": "api", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app-api", "container": "api"
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing release targets"):
                update_chart_values(values, load_release(release_dir))

    def test_fails_closed_for_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text("kind: Deployment\nmetadata:\n  name: other\n", encoding="utf-8")
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "api.json").write_text(json.dumps({
                "component": "api", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app-api", "container": "api"
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing release targets"):
                update(images, load_release(release_dir))


if __name__ == "__main__":
    unittest.main()
