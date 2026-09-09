from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]


class WorkflowContractTests(unittest.TestCase):
    def test_all_external_actions_are_versioned(self) -> None:
        sha40_re = re.compile(r"^[0-9a-f]{40}$")
        for path in (ROOT / ".github").rglob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for action in re.findall(r"^\s*uses:\s*([^\s]+)", text, re.MULTILINE):
                if action.startswith("./"):
                    continue
                self.assertIn("@", action, f"unversioned action in {path}: {action}")
                target, ref = action.split("@", 1)
                if not target.startswith("JustShinobi/platform-workflows"):
                    self.assertTrue(
                        sha40_re.match(ref),
                        f"action in {path} must be pinned to full 40-char commit SHA: {action}",
                    )

    def test_zot_publication_is_never_github_hosted(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertIn('runner=["arc-k3s"]', release)
        self.assertIn('runner=["self-hosted","proxmox-lxc","crossbuild"]', release)
        self.assertNotIn("runs-on: ubuntu", release)

    def test_no_workflow_uses_github_hosted_ubuntu(self) -> None:
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("runs-on: ubuntu", text, f"{path.name} still uses runs-on: ubuntu")

    def test_trivy_binary_version_is_explicit(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertRegex(release, r"uses: aquasecurity/trivy-action@[^\s]+[^\n]*\n\s+with:\n\s+version: v\d+\.\d+\.\d+")

    def test_sbom_evidence_retries_transient_download_failures(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertEqual(release.count("uses: anchore/sbom-action@"), 3)
        self.assertEqual(release.count("upload-artifact: false"), 3)
        self.assertEqual(release.count("upload-release-assets: false"), 3)
        self.assertIn("Validate SPDX SBOM evidence", release)

    def test_production_promotion_reuses_an_existing_branch(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertIn("Prepare production promotion branch", release)
        self.assertIn('git ls-remote --exit-code --heads origin "$branch"', release)
        self.assertIn('git switch -C "$branch" "origin/$branch"', release)
        self.assertIn("Production already references these digests", release)
        self.assertIn("printf 'branch=%s\\n' \"$branch\" >> \"$GITHUB_OUTPUT\"", release)

    def test_descriptor_does_not_accept_commands(self) -> None:
        validator = (ROOT / "scripts/validate_descriptor.py").read_text(encoding="utf-8")
        self.assertNotIn('"command"', validator)
    def test_workflow_call_secrets_do_not_contain_description(self) -> None:
        import yaml
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            # yaml evaluates "on" as True
            on_data = data.get(True) or data.get("on") or {}
            if isinstance(on_data, dict) and "workflow_call" in on_data:
                wf_call = on_data["workflow_call"] or {}
                secrets_data = wf_call.get("secrets") or {}
                for sec_name, sec_cfg in secrets_data.items():
                    if isinstance(sec_cfg, dict):
                        self.assertNotIn(
                            "description",
                            sec_cfg,
                            f"workflow_call.secrets.{sec_name} in {path.name} cannot contain 'description'",
                        )


    def test_if_conditionals_do_not_use_expression_braces(self) -> None:
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(
                text,
                r"^\s*if:\s*\$\{\{",
                f"workflow {path.name} uses redundant/invalid '${{{{ }}}}' syntax in 'if' conditional",
            )


if __name__ == "__main__":
    unittest.main()
