import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.update_gitops_images import (
    load_release,
    load_release_records,
    update,
    update_chart_values,
    update_t14,
)


class GitOpsImageTests(unittest.TestCase):
    @staticmethod
    def _fragment(
        component: str, image: str, digest_char: str, workload: str, container: str
    ) -> dict[str, str]:
        return {
            "component": component,
            "image": image,
            "digest": "sha256:" + digest_char * 64,
            "workload": workload,
            "container": container,
        }

    def test_updates_exact_workload_and_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text(
                "kind: Deployment\nmetadata:\n  name: app-api\nspec:\n  template:\n    spec:\n"
                "      containers:\n        - name: api\n          image: old@sha256:"
                + "0" * 64
                + "\n",
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "sbom-api.spdx.json").write_text(
                json.dumps({"spdxVersion": "SPDX-2.3"}), encoding="utf-8"
            )
            (release_dir / "api.json").write_text(
                json.dumps(
                    {
                        "component": "api",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app-api",
                        "container": "api",
                    }
                ),
                encoding="utf-8",
            )
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
            (release_dir / "api.json").write_text(
                json.dumps(
                    {
                        "component": "api",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app-api",
                        "container": "api",
                    }
                ),
                encoding="utf-8",
            )
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
            (release_dir / "api.json").write_text(
                json.dumps(
                    {
                        "component": "api",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app-api",
                        "container": "api",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing release targets"):
                update_chart_values(values, load_release(release_dir))

    def test_fails_closed_for_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text(
                "kind: Deployment\nmetadata:\n  name: other\n", encoding="utf-8"
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "api.json").write_text(
                json.dumps(
                    {
                        "component": "api",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app-api",
                        "container": "api",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing release targets"):
                update(images, load_release(release_dir))

    def test_updates_all_declared_t14_arms_by_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            documents = []
            targets = ["baseline", "proposed"]
            for target in targets:
                documents.append(
                    {
                        "kind": "Deployment",
                        "metadata": {"name": f"t14-{target}"},
                        "spec": {
                            "template": {
                                "metadata": {"labels": {"ninjasre.io/t14-arm": target}},
                                "spec": {
                                    "containers": [{"name": "app", "image": "old"}]
                                },
                            }
                        },
                    }
                )
            images.write_text(
                "---\n".join(
                    yaml.safe_dump(item, sort_keys=False) for item in documents
                ),
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "app.json").write_text(
                json.dumps(
                    {
                        "component": "app",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app",
                        "container": "app",
                    }
                ),
                encoding="utf-8",
            )
            baseline = {("app", "app"): "registry.lan/baseline@sha256:" + "b" * 64}
            update_t14(
                images,
                load_release(release_dir),
                targets,
                "app",
                baseline_release=baseline,
            )
            rendered = list(yaml.safe_load_all(images.read_text(encoding="utf-8")))
            self.assertEqual(
                rendered[0]["spec"]["template"]["spec"]["containers"][0]["image"],
                "registry.lan/baseline@sha256:" + "b" * 64,
            )
            self.assertEqual(
                rendered[1]["spec"]["template"]["spec"]["containers"][0]["image"],
                "registry.lan/app@sha256:" + "a" * 64,
            )

    def test_t14_updates_all_ten_image_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            targets = [
                "baseline",
                "proposed",
                "proposed-without-relations",
                "proposed-without-t06_readers",
                "proposed-without-t07_probes",
                "proposed-without-t09_planning",
            ]
            documents: list[dict[str, object]] = []
            for target in targets:
                documents.append(
                    {
                        "kind": "Deployment",
                        "metadata": {"name": f"t14-{target}"},
                        "spec": {
                            "template": {
                                "metadata": {"labels": {"ninjasre.io/t14-arm": target}},
                                "spec": {
                                    "containers": [{"name": "app", "image": "old"}],
                                },
                            },
                        },
                    }
                )
            documents.extend(
                [
                    {
                        "kind": "Deployment",
                        "metadata": {"name": "t14-proxy"},
                        "spec": {
                            "template": {
                                "metadata": {
                                    "labels": {"ninjasre.io/t14-role": "shared"}
                                },
                                "spec": {
                                    "containers": [{"name": "proxy", "image": "old"}],
                                },
                            },
                        },
                    },
                    {
                        "kind": "Job",
                        "metadata": {"name": "t14-observer"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {"name": "observer", "image": "old"}
                                    ],
                                },
                            },
                        },
                    },
                    {
                        "kind": "Job",
                        "metadata": {"name": "t14-judge-auditor"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "initContainers": [
                                        {"name": "stage-t14-judge-key", "image": "old"}
                                    ],
                                    "containers": [{"name": "auditor", "image": "old"}],
                                },
                            },
                        },
                    },
                ]
            )
            images.write_text(
                "---\n".join(
                    yaml.safe_dump(item, sort_keys=False) for item in documents
                ),
                encoding="utf-8",
            )
            release_dir = root / "release"
            release_dir.mkdir()
            for fragment in (
                self._fragment("app", "registry.lan/app", "a", "app", "app"),
                self._fragment("proxy", "registry.lan/proxy", "c", "proxy", "proxy"),
                self._fragment(
                    "t14-observer",
                    "registry.lan/t14-observer",
                    "d",
                    "t14-observer",
                    "observer",
                ),
            ):
                (release_dir / f"{fragment['component']}.json").write_text(
                    json.dumps(fragment), encoding="utf-8"
                )
            baseline = {("app", "app"): "registry.lan/app@sha256:" + "b" * 64}
            records = load_release_records(release_dir)
            update_t14(
                images,
                load_release(release_dir),
                targets,
                "app",
                shared_components=["proxy", "t14-observer"],
                baseline_release=baseline,
                release_records=records,
            )
            rendered = list(yaml.safe_load_all(images.read_text(encoding="utf-8")))
            image_refs = [
                container["image"]
                for document in rendered
                for container in [
                    *document.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("initContainers", []),
                    *document.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", []),
                ]
                if isinstance(container, dict) and "image" in container
            ]
            self.assertEqual(len(image_refs), 10)
            self.assertEqual(image_refs.count("registry.lan/app@sha256:" + "b" * 64), 1)
            self.assertEqual(image_refs.count("registry.lan/app@sha256:" + "a" * 64), 7)
            self.assertEqual(
                image_refs.count("registry.lan/proxy@sha256:" + "c" * 64), 1
            )
            self.assertEqual(
                image_refs.count("registry.lan/t14-observer@sha256:" + "d" * 64), 1
            )

    def test_common_update_can_allow_t14_only_component_to_be_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text(
                "kind: Deployment\nmetadata:\n  name: app\nspec:\n  template:\n    spec:\n"
                "      containers:\n      - name: app\n        image: old\n",
                encoding="utf-8",
            )
            release = {
                ("app", "app"): "registry.lan/app@sha256:" + "a" * 64,
                ("t14-observer", "observer"): "registry.lan/t14-observer@sha256:"
                + "b" * 64,
            }
            update(
                images, release, allow_missing_targets={("t14-observer", "observer")}
            )
            data = yaml.safe_load(images.read_text(encoding="utf-8"))
            self.assertEqual(
                data["spec"]["template"]["spec"]["containers"][0]["image"],
                "registry.lan/app@sha256:" + "a" * 64,
            )

    def test_common_update_allows_two_t14_only_components_but_requires_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            release_dir = root / "release"
            release_dir.mkdir()
            fragments = (
                self._fragment("app", "registry.lan/app", "a", "app", "app"),
                self._fragment("proxy", "registry.lan/proxy", "b", "proxy", "proxy"),
                self._fragment(
                    "t14-observer", "registry.lan/observer", "c", "t14-observer", "observer"
                ),
                self._fragment(
                    "t14-readonly-executor",
                    "registry.lan/executor",
                    "d",
                    "t14-readonly-executor",
                    "t14-readonly-executor",
                ),
            )
            for fragment in fragments:
                (release_dir / f"{fragment['component']}.json").write_text(
                    json.dumps(fragment), encoding="utf-8"
                )

            def common_images(*workloads: str) -> str:
                documents = [
                    {
                        "kind": "Deployment",
                        "metadata": {"name": workload},
                        "spec": {
                            "template": {
                                "spec": {"containers": [{"name": workload, "image": "old"}]}
                            }
                        },
                    }
                    for workload in workloads
                ]
                return "---\n".join(
                    yaml.safe_dump(document, sort_keys=False) for document in documents
                )

            command = [
                sys.executable,
                str(Path(__file__).parents[1] / "scripts/update_gitops_images.py"),
                str(images),
                str(release_dir),
                "--allow-missing-components",
                "t14-observer",
                "t14-readonly-executor",
            ]
            images.write_text(common_images("app", "proxy"), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = list(yaml.safe_load_all(images.read_text(encoding="utf-8")))
            self.assertEqual(
                [item["spec"]["template"]["spec"]["containers"][0]["image"] for item in updated],
                ["registry.lan/app@sha256:" + "a" * 64, "registry.lan/proxy@sha256:" + "b" * 64],
            )

            missing_proxy = common_images("app")
            images.write_text(missing_proxy, encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("proxy/proxy", result.stderr)
            self.assertEqual(images.read_text(encoding="utf-8"), missing_proxy)

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
            (release_dir / "app.json").write_text(
                json.dumps(
                    {
                        "component": "app",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app",
                        "container": "app",
                    }
                ),
                encoding="utf-8",
            )
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
            (release_dir / "app.json").write_text(
                json.dumps(
                    {
                        "component": "app",
                        "image": "registry.lan/app",
                        "digest": "sha256:" + "a" * 64,
                        "workload": "app",
                        "container": "app",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing T14 targets"):
                update_t14(
                    images,
                    load_release(release_dir),
                    ["baseline", "proposed"],
                    "app",
                    baseline_release={
                        ("app", "app"): "registry.lan/baseline@sha256:" + "b" * 64
                    },
                )


if __name__ == "__main__":
    unittest.main()
