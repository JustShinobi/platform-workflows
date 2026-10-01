import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

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

    def test_t14_build_only_requires_explicit_workflow_call_input(self) -> None:
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        )
        on_data = workflow.get(True) or workflow.get("on") or {}
        build_only = on_data["workflow_call"]["inputs"]["t14_build_only"]
        self.assertEqual(
            build_only,
            {
                "description": "Build unsigned T14 proposed evidence for separate host verification",
                "required": False,
                "default": False,
                "type": "boolean",
            },
        )
        source_revision = on_data["workflow_call"]["inputs"]["source_revision"]
        self.assertEqual(
            source_revision,
            {
                "description": "Full application commit SHA to build; defaults to the caller commit",
                "required": False,
                "default": "",
                "type": "string",
            },
        )
        prepare = workflow["jobs"]["prepare"]
        self.assertEqual(
            prepare["outputs"]["t14_build_only"], "${{ inputs.t14_build_only }}"
        )
        self.assertEqual(
            prepare["outputs"]["source_revision"], "${{ steps.source_revision.outputs.sha }}"
        )
        checkout = next(step for step in prepare["steps"] if step.get("name") == "Check out application")
        self.assertEqual(checkout["id"], "application")
        self.assertEqual(checkout["with"]["ref"], "${{ inputs.source_revision || github.sha }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)
        guard = next(
            step for step in prepare["steps"] if step.get("name") == "Require T14 build-only mode"
        )
        self.assertEqual(guard["if"], "steps.descriptor.outputs.t14_enabled == 'true'")
        self.assertEqual(guard["env"]["T14_BUILD_ONLY"], "${{ inputs.t14_build_only }}")
        self.assertIn("host verification", guard["run"])
        for value, expected_code in (("false", 2), ("true", 0)):
            with self.subTest(value=value):
                env = os.environ.copy()
                env["T14_BUILD_ONLY"] = value
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", guard["run"]],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code, result.stderr)

        revision_guard = next(
            step for step in prepare["steps"] if step.get("name") == "Validate source revision input"
        )
        for value, expected_code in (("", 0), ("a" * 40, 0), ("main", 2)):
            with self.subTest(source_revision=value):
                env = os.environ.copy()
                env["SOURCE_REVISION"] = value
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", revision_guard["run"]],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code, result.stderr)

        revision_step = next(
            step for step in prepare["steps"] if step.get("name") == "Record checked-out source revision"
        )
        self.assertEqual(revision_step["id"], "source_revision")
        self.assertIn("git rev-parse HEAD", revision_step["run"])
        component_guard = next(
            step
            for step in prepare["steps"]
            if step.get("name") == "Require app component for build-only evidence"
        )
        self.assertEqual(component_guard["if"], "inputs.t14_build_only == true")
        for matrix, expected_code in (
            ('{"include":[{"name":"app"}]}', 0),
            ('{"include":[{"name":"api"}]}', 2),
        ):
            with self.subTest(matrix=matrix):
                env = os.environ.copy()
                env["MATRIX"] = matrix
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", component_guard["run"]],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code, result.stderr)
        for job_name in ("verify", "build"):
            job = workflow["jobs"][job_name]
            source_checkout = next(
                step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
            )
            self.assertEqual(source_checkout["with"]["ref"], "${{ needs.prepare.outputs.source_revision }}")
            self.assertIs(source_checkout["with"]["persist-credentials"], False)

        smoke_checkout = next(
            step
            for step in workflow["jobs"]["promote-staging"]["steps"]
            if step.get("name") == "Check out application for smoke test"
        )
        self.assertIs(smoke_checkout["with"]["persist-credentials"], False)

        for job_name, checkout_name in (
            ("promote-staging", "Check out validated GitOps source branch"),
            ("propose-production", None),
        ):
            gitops_checkout = next(
                step
                for step in workflow["jobs"][job_name]["steps"]
                if step.get("uses", "").startswith("actions/checkout@")
                and (checkout_name is None or step.get("name") == checkout_name)
            )
            self.assertNotEqual(gitops_checkout.get("with", {}).get("persist-credentials"), False)

    def test_t14_build_artifact_is_exact_unsigned_schema_three_receipt_and_oci(self) -> None:
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        )
        build = workflow["jobs"]["build"]
        build_text = yaml.safe_dump(build, sort_keys=True).lower()
        for forbidden in (
            "private-key",
            "t14_key_path",
            "--private-key-file",
            "t14_builder_attestation.py",
            "t14-builder-proof-",
        ):
            self.assertNotIn(forbidden, build_text)

        steps = build["steps"]
        receipt_step = next(
            step
            for step in steps
            if step.get("name") == "Create unsigned T14 proposed artifact"
        )
        self.assertIn("needs.prepare.outputs.t14_build_only == 'true'", receipt_step["if"])
        self.assertIn("matrix.name == 'app'", receipt_step["if"])
        self.assertNotIn("t14_enabled", receipt_step["if"])
        self.assertIn("github.workflow_sha", str(receipt_step["env"]))
        self.assertIn("github.workflow_ref", str(receipt_step["env"]))
        self.assertIn("github.run_id", str(receipt_step["env"]))
        self.assertIn("github.run_attempt", str(receipt_step["env"]))
        self.assertIn("needs.prepare.outputs.source_revision", str(receipt_step["env"]))
        self.assertNotIn("T14_TARGETS", receipt_step["env"])
        self.assertNotIn("targets[@]", receipt_step["run"])
        self.assertNotIn("proposed_count", receipt_step["run"])
        image_build = next(
            step for step in steps if step.get("name") == "Build and publish immutable candidate"
        )
        self.assertIn(
            "${{ needs.prepare.outputs.source_revision }}", image_build["with"]["tags"]
        )

        upload = next(
            step for step in steps if step.get("name") == "Upload unsigned T14 proposed artifact"
        )
        self.assertEqual(upload["uses"], "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a")
        self.assertEqual(upload["with"]["name"], "t14-proposed-release-app")
        self.assertIs(upload["with"]["include-hidden-files"], True)
        artifact_paths = [line.strip() for line in upload["with"]["path"].splitlines() if line.strip()]
        self.assertEqual(
            artifact_paths,
            [
                ".ci/t14/release-t14-proposed-app.oci.tar",
                ".ci/t14/release-t14-proposed-app.json",
            ],
        )
        summary = next(
            step
            for step in steps
            if step.get("name") == "Report deferred T14 host promotion"
        )
        self.assertIn("t14-proposed-release-app", summary["run"])

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("t14-proposed-release-app", readme)
        self.assertIn("workflow chamador", readme)
        self.assertNotIn("revisão do workflow reutilizável", readme)

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "config", "user.email", "ci-contract@example.invalid"],
                cwd=checkout,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "CI Contract"], cwd=checkout, check=True
            )
            (checkout / "uv.lock").write_bytes(b"version = 1\n")
            (checkout / "application.txt").write_text("committed source\n", encoding="utf-8")
            subprocess.run(["git", "add", "uv.lock", "application.txt"], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=checkout, check=True)
            application_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            layout = checkout / ".ci/t14/oci-app"
            (layout / "blobs/sha256").mkdir(parents=True)
            (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
            (layout / "index.json").write_text('{"schemaVersion":2,"manifests":[]}', encoding="utf-8")
            (layout / "blobs/sha256/example").write_bytes(b"oci blob")

            env = os.environ.copy()
            env.update(
                {
                    "T14_IMAGE_DIGEST": "sha256:" + "a" * 64,
                    "T14_APPLICATION_REVISION": application_revision,
                    "T14_WORKFLOW_REVISION": "b" * 40,
                    "T14_WORKFLOW_REF": "JustShinobi/NinjaSRE-private/.github/workflows/application-ci.yml@refs/heads/main",
                    "T14_RUN_ID": "123456",
                    "T14_RUN_ATTEMPT": "1",
                    "T14_OCI_LAYOUT": ".ci/t14/oci-app",
                    "T14_OCI_ARCHIVE": ".ci/t14/release-t14-proposed-app.oci.tar",
                    "T14_RECEIPT": ".ci/t14/release-t14-proposed-app.json",
                }
            )
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", receipt_step["run"]],
                cwd=checkout,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            archive_path = checkout / env["T14_OCI_ARCHIVE"]
            receipt_path = checkout / env["T14_RECEIPT"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(receipt),
                {
                    "schema_version",
                    "target_name",
                    "workflow_revision",
                    "workflow_ref",
                    "workflow_run_id",
                    "run_attempt",
                    "application_revision",
                    "image_digest",
                    "oci_archive_sha256",
                    "source_sha256",
                    "lock_sha256",
                    "proof_status",
                },
            )
            self.assertEqual(receipt["schema_version"], 3)
            self.assertEqual(receipt["target_name"], "proposed")
            self.assertEqual(receipt["workflow_revision"], "b" * 40)
            self.assertEqual(receipt["workflow_ref"], env["T14_WORKFLOW_REF"])
            self.assertEqual(receipt["workflow_run_id"], "123456")
            self.assertEqual(receipt["run_attempt"], 1)
            self.assertEqual(receipt["application_revision"], application_revision)
            self.assertEqual(receipt["image_digest"], "sha256:" + "a" * 64)
            self.assertEqual(
                receipt["oci_archive_sha256"],
                "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            )
            source_archive = subprocess.run(
                ["git", "archive", "--format=tar", "HEAD"],
                cwd=checkout,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(receipt["source_sha256"], hashlib.sha256(source_archive).hexdigest())
            self.assertEqual(
                receipt["lock_sha256"], hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()
            )
            self.assertEqual(receipt["proof_status"], "unsigned-awaiting-host-attestor")
            with tarfile.open(archive_path, "r:") as archive:
                member_names = {member.name.rstrip("/") for member in archive.getmembers()}
            self.assertEqual(
                {name.split("/", maxsplit=1)[0] for name in member_names},
                {"oci-layout", "index.json", "blobs"},
            )
            self.assertIn("oci-layout", member_names)
            self.assertIn("index.json", member_names)
            self.assertIn("blobs/sha256/example", member_names)

    def test_t14_promotion_consumes_a_pinned_cross_repository_baseline_build(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertNotIn("Block T14 until a separate historical baseline build is available", release)
        self.assertIn("t14_baseline_repository", release)
        self.assertIn("t14_baseline_run_id", release)
        self.assertIn("t14_baseline_artifact_name", release)
        self.assertIn("t14_baseline_workflow_sha", release)
        self.assertIn("t14-baseline-release-file", release)
        self.assertIn("t14-proof-directory", release)
        self.assertIn("t14-builder-public-key-file", release)

    def test_t14_release_pins_actions_with_workflow_revision_support(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        provenance_revision = "b282ff4cee929496450d58a2909c8ad28b84b9fc"
        descriptor = (
            "JustShinobi/platform-workflows/.github/actions/descriptor@"
            + provenance_revision
        )
        updater = (
            "JustShinobi/platform-workflows/.github/actions/update-gitops-images@"
            + provenance_revision
        )
        self.assertIn(f"uses: {descriptor}", release)
        update_uses = re.findall(
            r"uses: (JustShinobi/platform-workflows/\.github/actions/update-gitops-images@\S+)",
            release,
        )
        self.assertEqual(update_uses, [updater] * 3)
        self.assertIn(
            "t14_baseline_workflow_sha:",
            (ROOT / ".github/actions/descriptor/action.yml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "t14-baseline-workflow-sha:",
            (ROOT / ".github/actions/update-gitops-images/action.yml").read_text(
                encoding="utf-8"
            ),
        )

    def test_t14_build_only_report_defers_host_attestation_and_promotion(self) -> None:
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["build"]["steps"]
        report = next(
            step
            for step in steps
            if step.get("name") == "Report deferred T14 host promotion"
        )
        self.assertIn("needs.prepare.outputs.t14_build_only == 'true'", report["if"])
        self.assertIn("unsigned proposed artifact", report["run"])
        self.assertIn("host verification", report["run"])
        self.assertIn("five proposed target envelopes", report["run"])
        self.assertIn("separate promotion", report["run"])

    def test_t14_build_only_skips_staging_health_and_production_jobs(self) -> None:
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        )
        staging = workflow["jobs"]["promote-staging"]
        production = workflow["jobs"]["propose-production"]
        self.assertIn("needs.prepare.outputs.t14_build_only != 'true'", staging["if"])
        self.assertIn("needs.prepare.outputs.promotion_mode != 'production-only'", staging["if"])
        self.assertIn("needs.prepare.outputs.t14_build_only != 'true'", production["if"])
        self.assertIn("needs.prepare.outputs.t14_enabled != 'true'", production["if"])
        self.assertIn(
            "needs.promote-staging.outputs.t14_application_activated == 'true'", production["if"]
        )
        self.assertNotIn("production-only", workflow["jobs"]["build"]["name"])

    def test_non_t14_staging_publication_keeps_the_base_branch(self) -> None:
        release = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        staging = release.split("  promote-staging:", 1)[1].split("  propose-production:", 1)[0]
        self.assertIn("|| needs.prepare.outputs.gitops_base_branch }}", staging)
        self.assertNotIn("ref: ${{ needs.prepare.outputs.gitops_staging_branch }}", staging)
        self.assertNotIn(
            "TARGET_BRANCH: ${{ needs.prepare.outputs.gitops_staging_branch }}",
            staging,
        )

    def test_only_t14_only_components_may_be_missing_from_common_image_files(self) -> None:
        import yaml

        release = yaml.safe_load(
            (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        )
        expected = "${{ needs.prepare.outputs.t14_only_components || '' }}"
        for job_name, step_name in (
            ("promote-staging", "Update staging image patches"),
            ("propose-production", "Update production image patches"),
        ):
            with self.subTest(job=job_name):
                steps = release["jobs"][job_name]["steps"]
                update = next(step for step in steps if step.get("name") == step_name)
                self.assertEqual(update["with"]["allow-missing-components"], expected)

    def test_t14_baseline_fetch_binds_run_revision_and_application_source(self) -> None:
        action = (ROOT / ".github/actions/update-gitops-images/action.yml").read_text(encoding="utf-8")
        self.assertIn("actions/runs/$T14_BASELINE_RUN_ID/artifacts", action)
        self.assertIn("workflow_run.id", action)
        self.assertIn("workflow_run.head_sha", action)
        self.assertIn("--arg workflow_sha", action)
        self.assertIn(".workflow_run.head_sha == $workflow_sha", action)
        self.assertNotIn(".workflow_run.head_sha == $source_sha", action)
        self.assertIn("validate_t14_baseline_artifact.py", action)
        self.assertIn('--expected-source-sha "$T14_BASELINE_SOURCE_SHA"', action)
        self.assertIn("--expected-workflow-sha", action)
        self.assertIn("docker load --input", action)
        self.assertIn("docker push", action)
        self.assertIn('test "$loaded_image" = "$baseline_digest"', action)
        self.assertIn("pushed_digest", action)
        self.assertIn('test "$remote_digest" = "$baseline_digest"', action)
        self.assertIn("T14_BASELINE_SOURCE_SHA", action)
        self.assertIn("T14_BASELINE_WORKFLOW_SHA", action)

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
                self.assertIn("runs-on: arc-k3s-platform-workflows", text)
            self.assertNotIn("runs-on: ubuntu", text, f"{path.name} still uses runs-on: ubuntu")

    def test_platform_pr_validation_requires_a_branch_in_this_repository(self) -> None:
        workflow = (ROOT / ".github/workflows/self-test.yml").read_text(encoding="utf-8")
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", workflow)

    def test_standalone_secret_sync_uses_the_platform_arc_scale_set(self) -> None:
        workflow = (ROOT / ".github/workflows/sync-secrets-infisical.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("runs-on: arc-k3s-platform-workflows", workflow)
        self.assertIn("INFISICAL_CLIENT_SECRET: ${{ secrets.INFISICAL_CLIENT_SECRET }}", workflow)
        self.assertNotIn("secrets.INFISICAL_CLIENT_SECRET ||", workflow)

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
