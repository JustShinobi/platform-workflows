import json
from pathlib import Path
import tempfile
import unittest

import yaml

from scripts.update_gitops_images import (
    load_release,
    update,
    update_chart_values,
    update_t14,
)


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

    def test_updates_all_declared_t14_arms_by_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            documents = []
            targets = ["baseline", "proposed"]
            for target in targets:
                documents.append({
                    "kind": "Deployment",
                    "metadata": {"name": f"t14-{target}"},
                    "spec": {"template": {"metadata": {"labels": {"ninjasre.io/t14-arm": target}},
                        "spec": {"containers": [{"name": "app", "image": "old"}]}}},
                })
            images.write_text("---\n".join(yaml.safe_dump(item, sort_keys=False) for item in documents), encoding="utf-8")
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "app.json").write_text(json.dumps({
                "component": "app", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app", "container": "app"
            }), encoding="utf-8")
            baseline = {("app", "app"): "registry.lan/baseline@sha256:" + "b" * 64}
            update_t14(images, load_release(release_dir), targets, "app", baseline_release=baseline)
            rendered = list(yaml.safe_load_all(images.read_text(encoding="utf-8")))
            self.assertEqual(
                rendered[0]["spec"]["template"]["spec"]["containers"][0]["image"],
                "registry.lan/baseline@sha256:" + "b" * 64,
            )
            self.assertEqual(
                rendered[1]["spec"]["template"]["spec"]["containers"][0]["image"],
                "registry.lan/app@sha256:" + "a" * 64,
            )

    def test_t14_update_requires_distinct_baseline_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text(
                "kind: Deployment\nmetadata:\n  name: t14-baseline\nspec:\n"
                "  template:\n    metadata:\n      labels:\n        ninjasre.io/t14-arm: baseline\n"
                "    spec:\n      containers:\n      - name: app\n        image: old\n",
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "app.json").write_text(json.dumps({
                "component": "app", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app", "container": "app"
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "separate baseline release"):
                update_t14(images, load_release(release_dir), ["baseline"], "app")

    def test_t14_update_fails_closed_for_missing_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text(
                "kind: Deployment\nmetadata:\n  name: t14-baseline\nspec:\n"
                "  template:\n    metadata:\n      labels:\n        ninjasre.io/t14-arm: baseline\n"
                "    spec:\n      containers:\n      - name: app\n        image: old\n",
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "app.json").write_text(json.dumps({
                "component": "app", "image": "registry.lan/app", "digest": "sha256:" + "a" * 64,
                "workload": "app", "container": "app"
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing T14 targets"):
                update_t14(
                    images,
                    load_release(release_dir),
                    ["baseline", "proposed"],
                    "app",
                    baseline_release={("app", "app"): "registry.lan/baseline@sha256:" + "b" * 64},
                )


if __name__ == "__main__":
    unittest.main()
