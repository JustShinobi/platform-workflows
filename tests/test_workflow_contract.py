import os
from pathlib import Path
import re
import subprocess
import tempfile
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
        self.assertIn('runner=["arc-k3s-ninjasre-t14"]', release)
        self.assertIn('runner=["self-hosted","proxmox-lxc","crossbuild"]', release)
        self.assertNotIn("runs-on: ubuntu", release)

    def test_t14_builder_proof_is_bound_to_checkout_lock_and_oci_digest(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertIn("--repository-root .", release)
        self.assertIn("--lock-file uv.lock", release)
        self.assertIn("--oci-layout", release)
        self.assertIn("--image-digest \"$T14_IMAGE_DIGEST\"", release)
        self.assertIn("push: true", release)
        self.assertIn("type=oci,dest=.ci/t14/oci-", release)
        self.assertIn("test \"${#targets[@]}\" -eq 6", release)
        self.assertIn('if [ "$target" = baseline ]; then', release)
        self.assertIn('test "$proposed_count" -eq 5', release)
        self.assertIn("name: t14-builder-proof-${{ matrix.name }}", release)

    def test_t14_promotion_consumes_a_pinned_cross_repository_baseline_build(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertNotIn("Block T14 until a separate historical baseline build is available", release)
        self.assertIn("t14_baseline_repository", release)
        self.assertIn("t14_baseline_run_id", release)
        self.assertIn("t14_baseline_artifact_name", release)
        self.assertIn("t14-baseline-release-file", release)
        self.assertIn("t14-proof-directory", release)
        self.assertIn("t14-builder-public-key-file", release)

    def test_t14_publication_only_mode_is_explicit_when_application_is_absent(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        activation = release.split("- name: Detect T14 Application activation", 1)[1]
        self.assertIn("http_code=\"$(curl", activation)
        self.assertIn("404)", activation)
        self.assertIn("activated=false", activation)
        self.assertIn("publication_only=true", activation)
        self.assertIn("T14 publication-only", activation)
        self.assertIn("Argo health and production promotion are deferred", activation)
        self.assertIn("*)", activation)
        self.assertIn("Could not determine whether T14 Application", activation)
        self.assertIn("exit 1", activation)
        self.assertNotIn('cat "$response_file"', activation)

    def test_t14_active_application_keeps_both_health_and_promotion_gates(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        activation = release.split("- name: Detect T14 Application activation", 1)[1]
        self.assertIn("200)", activation)
        self.assertIn("activated=true", activation)
        t14_health = release.split("- name: Wait for Argo CD T14 health", 1)[1]
        self.assertIn("steps.t14_activation.outputs.activated == 'true'", t14_health)

    def test_t14_absent_application_cannot_reach_production_promotion(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        production = release.split("  propose-production:", 1)[1]
        self.assertIn("needs.prepare.outputs.t14_enabled != 'true'", production)
        self.assertIn("needs.promote-staging.outputs.t14_application_activated == 'true'", production)

    def test_t14_publication_only_does_not_bypass_proof_validation(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        update_index = release.index("- name: Update T14 image patches")
        activation_index = release.index("- name: Detect T14 Application activation")
        self.assertLess(update_index, activation_index)
        action = (ROOT / ".github/actions/update-gitops-images/action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("validate_t14_promotion.py", action)
        self.assertIn("test -d \"$T14_PROOF_DIRECTORY\"", action)
        self.assertIn("test -s \"$T14_BASELINE_RELEASE_FILE\"", action)

    def test_non_t14_staging_publication_keeps_the_base_branch(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        staging = release.split("  promote-staging:", 1)[1].split("  propose-production:", 1)[0]
        self.assertIn("|| needs.prepare.outputs.gitops_base_branch }}", staging)
        self.assertNotIn("ref: ${{ needs.prepare.outputs.gitops_staging_branch }}", staging)
        self.assertNotIn(
            "TARGET_BRANCH: ${{ needs.prepare.outputs.gitops_staging_branch }}",
            staging,
        )

    def test_t14_staging_publication_selects_its_validated_branch(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        staging = release.split("  promote-staging:", 1)[1].split("  propose-production:", 1)[0]
        branch = (
            "needs.prepare.outputs.t14_enabled == 'true' && "
            "needs.prepare.outputs.gitops_staging_branch || "
            "needs.prepare.outputs.gitops_base_branch"
        )
        self.assertIn(f"ref: ${{{{ {branch} }}}}", staging)
        self.assertIn(f"TARGET_BRANCH: ${{{{ {branch} }}}}", staging)

    def test_t14_baseline_fetch_binds_artifact_to_run_and_source_sha(self) -> None:
        action = (ROOT / ".github/actions/update-gitops-images/action.yml").read_text(encoding="utf-8")
        self.assertIn("actions/runs/$T14_BASELINE_RUN_ID/artifacts", action)
        self.assertIn("workflow_run.id", action)
        self.assertIn("workflow_run.head_sha", action)
        self.assertIn("validate_t14_baseline_artifact.py", action)
        self.assertIn("docker load --input", action)
        self.assertIn("docker push", action)
        self.assertIn('test "$loaded_image" = "$baseline_digest"', action)
        self.assertIn("pushed_digest", action)
        self.assertIn('test "$remote_digest" = "$baseline_digest"', action)
        self.assertIn("T14_BASELINE_SOURCE_SHA", action)

    def test_t14_image_action_requires_proof_inputs_before_writing_images(self) -> None:
        action = (ROOT / ".github/actions/update-gitops-images/action.yml").read_text(
            encoding="utf-8"
        )
        apply_step = action.split("      run: |", 1)[1]
        write_index = apply_step.index('          "${command[@]}"')
        for guard in (
            'if [ -z "$T14_PROOF_DIRECTORY" ]',
            '[ -z "$T14_BUILDER_KEY_ID" ]',
            '[ -z "$T14_BASELINE_SOURCE_SHA" ]',
            '[ -z "$T14_PROPOSED_SOURCE_SHA" ]',
            '[ -z "$T14_BUILDER_PUBLIC_KEY_FILE" ]',
            '[ -z "$T14_BASELINE_RELEASE_FILE" ]',
            'test -d "$T14_PROOF_DIRECTORY"',
            'test -s "$T14_BUILDER_PUBLIC_KEY_FILE"',
        ):
            self.assertLess(apply_step.index(guard), write_index, guard)
        validation_index = apply_step.index("validate_t14_promotion.py")
        self.assertLess(validation_index, write_index)
        self.assertNotIn('if [ -n "$T14_PROOF_DIRECTORY" ]; then', apply_step)

    def test_t14_image_action_rejects_omitted_proof_directory_before_mutation(self) -> None:
        import yaml

        action_data = yaml.safe_load(
            (ROOT / ".github/actions/update-gitops-images/action.yml").read_text(
                encoding="utf-8"
            )
        )
        run = action_data["runs"]["steps"][-1]["run"].replace(
            "${{ github.action_path }}", str(ROOT / ".github/actions/update-gitops-images")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images.yaml"
            images.write_text("sentinel\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "IMAGES_FILE": str(images),
                    "RELEASE_DIRECTORY": str(root / "release"),
                    "T14_TARGETS": "baseline proposed",
                    "T14_COMPONENT": "app",
                    "T14_SHARED_COMPONENTS": "",
                    "T14_BASELINE_RELEASE_FILE": str(root / "baseline.json"),
                    "T14_PROOF_DIRECTORY": "",
                    "T14_BUILDER_KEY_ID": "builder",
                    "T14_BASELINE_SOURCE_SHA": "a" * 40,
                    "T14_PROPOSED_SOURCE_SHA": "b" * 40,
                    "T14_BUILDER_PUBLIC_KEY_FILE": str(root / "builder.pub"),
                }
            )
            result = subprocess.run(
                ["bash", "-c", run],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(images.read_text(encoding="utf-8"), "sentinel\n")

    def test_no_workflow_uses_github_hosted_ubuntu(self) -> None:
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            if path.name == "self-test.yml":
                self.assertIn("runs-on: ubuntu-latest", text)
                continue
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
