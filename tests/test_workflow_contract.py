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
        self.assertIn('runner=["arc-k3s-heavy"]', release)
        self.assertIn('runner=["arc-k3s-medium"]', release)
        self.assertIn('runner=["arc-k3s-light"]', release)
        self.assertIn('runner=["arc-k3s-ninjasre-t14"]', release)
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
        provenance_revision = "141795287a15a540b5396914d99247a1f8f82d8e"
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
        self.assertIn("always()", staging["if"])
        self.assertIn("needs.prepare.result == 'success'", staging["if"])
        self.assertIn("needs.build.result == 'success'", staging["if"])
        self.assertIn("needs.prepare.outputs.t14_build_only != 'true'", staging["if"])
        self.assertIn("needs.prepare.outputs.promotion_mode != 'production-only'", staging["if"])
        self.assertIn("needs.prepare.outputs.t14_build_only != 'true'", production["if"])
        self.assertIn(
            "(needs.promote-staging.result == 'skipped' && needs.prepare.outputs.promotion_mode == 'production-only')",
            production["if"],
        )
        self.assertIn("needs.prepare.outputs.t14_enabled != 'true'", production["if"])
        self.assertIn(
            "needs.promote-staging.outputs.t14_application_activated == 'true'", production["if"]
        )
        self.assertNotIn("production-only", workflow["jobs"]["build"]["name"])
        smoke_setup_node = next(
            step
            for step in staging["steps"]
            if step.get("name") == "Set up Node.js runtime"
        )
        self.assertIs(smoke_setup_node["with"]["package-manager-cache"], False)

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
                self.assertIn("runs-on: arc-k3s-light", text)
            self.assertNotIn("runs-on: ubuntu", text, f"{path.name} still uses runs-on: ubuntu")

    def test_platform_pr_validation_requires_a_branch_in_this_repository(self) -> None:
        workflow = (ROOT / ".github/workflows/self-test.yml").read_text(encoding="utf-8")
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", workflow)

    def test_standalone_secret_sync_uses_the_platform_arc_scale_set(self) -> None:
        workflow = (ROOT / ".github/workflows/sync-secrets-infisical.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("runs-on: arc-k3s-light", workflow)
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

    def test_production_promotion_reuses_rolling_branch_and_updates_pr_in_place(self) -> None:
        import http.server
        import threading
        import yaml

        release_text = (ROOT / ".github/workflows/application-release.yml").read_text(encoding="utf-8")
        self.assertIn("Prepare production promotion branch", release_text)
        self.assertIn('branch="promote/${APPLICATION}/production"', release_text)
        self.assertIn('--force-with-lease="refs/heads/${branch}:${remote_oid}"', release_text)
        self.assertIn("Production already references these digests", release_text)
        self.assertIn("printf 'branch=%s\\n' \"$branch\"", release_text)
        self.assertIn("const prefix = `promote/${app}/`;", release_text)
        self.assertIn("github.rest.pulls.update", release_text)
        self.assertIn("github.rest.git.deleteRef({ owner, repo, ref: `heads/${prRef}` })", release_text)

        workflow = yaml.safe_load(release_text)
        prod_steps = workflow["jobs"]["propose-production"]["steps"]
        baseline_step = next(s for s in prod_steps if s.get("name") == "Capture current production baseline")
        branch_step = next(s for s in prod_steps if s.get("name") == "Prepare production promotion branch")
        pr_step = next(s for s in prod_steps if s.get("name") == "Create or update production pull request")
        stg_guard_step = next(
            s
            for s in workflow["jobs"]["promote-staging"]["steps"]
            if s.get("name") == "Guard against out-of-order staging downgrade"
        )

        sha0 = "0" * 40
        sha1 = "1" * 40
        sha2 = "2" * 40
        sha2_5 = "d" * 40
        sha3 = "3" * 40
        commit_order = [sha0, sha1, sha2, sha2_5, sha3]
        state_holder: dict[str, Path] = {}

        class CompareHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if re.fullmatch(r"/repos/[^/]+/k3s-gitops-prod", self.path):
                    body = json.dumps({"full_name": "Shhinobi/k3s-gitops-prod"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/repos/Shhinobi/k3s-gitops-prod/pulls") or self.path.startswith(
                    "/repos/JustShinobi/k3s-gitops-prod/pulls"
                ):
                    st_file = state_holder.get("path")
                    prs = []
                    if st_file and st_file.exists():
                        st = json.loads(st_file.read_text(encoding="utf-8"))
                        prs = [p for p in st.get("prs", []) if p.get("state") == "open"]
                    body = json.dumps(prs).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                m = re.search(r"/repos/[^/]+/[^/]+/compare/([^.]+)\.\.\.(.+)$", self.path)
                if not m:
                    self.send_response(404)
                    self.end_headers()
                    return
                base_ref, head_ref = m.group(1), m.group(2)
                base_full = next((c for c in commit_order if c.startswith(base_ref)), None)
                head_full = next((c for c in commit_order if c.startswith(head_ref)), None)
                if base_full is None or head_full is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                b_idx = commit_order.index(base_full)
                h_idx = commit_order.index(head_full)
                if h_idx > b_idx:
                    status = "ahead"
                    commits = [
                        {"sha": c, "commit": {"message": f"feat(runa): commit {i}\n\nbody"}}
                        for i, c in enumerate(commit_order[b_idx + 1 : h_idx + 1], start=b_idx + 1)
                    ]
                elif h_idx == b_idx:
                    status = "identical"
                    commits = []
                else:
                    status = "behind"
                    commits = []
                payload = json.dumps(
                    {
                        "status": status,
                        "base_commit": {"sha": base_full},
                        "total_commits": len(commits),
                        "commits": commits,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CompareHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        api_url = f"http://127.0.0.1:{server.server_port}"

        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                origin = root / "origin.git"
                seed = root / "seed"
                runner_temp = root / "runner_temp"
                runner_temp.mkdir()
                subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)], check=True)
                subprocess.run(["git", "clone", "--quiet", str(origin), str(seed)], check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=seed, check=True)
                subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=seed, check=True)

                prod_rel = "clusters/prod/workloads/runa-prd/images.yaml"
                stg_rel = "clusters/prod/workloads/runa-stg/images.yaml"
                (seed / prod_rel).parent.mkdir(parents=True)
                (seed / stg_rel).parent.mkdir(parents=True)

                def render_img(char: str) -> str:
                    return (
                        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"
                        "spec:\n  template:\n    spec:\n      containers:\n"
                        f"      - name: api\n        image: registry.lan.kyo.ninja/runa/api@sha256:{char * 64}\n"
                    )

                (seed / prod_rel).write_text(render_img("0"), encoding="utf-8")
                (seed / stg_rel).write_text(render_img("2"), encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=seed, check=True)
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "--quiet",
                        "-m",
                        f"chore(runa): promote {sha0[:12]} to production",
                        "-m",
                        f"Source-Revision: {sha2}",
                    ],
                    cwd=seed,
                    check=False,
                )
                # Separate staging commit so staging tracks sha2 while prod tracks sha0
                (seed / prod_rel).write_text(render_img("0"), encoding="utf-8")
                subprocess.run(["git", "commit", "--amend", "--quiet", "-m", f"chore(runa): promote {sha0[:12]} to production"], cwd=seed, check=True)
                (seed / stg_rel).write_text(render_img("a"), encoding="utf-8")
                subprocess.run(["git", "add", stg_rel], cwd=seed, check=True)
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "--quiet",
                        "-m",
                        f"chore(runa): promote {sha2[:12]} to staging",
                        "-m",
                        "Source-Repository: Shhinobi/Runa",
                        "-m",
                        f"Source-Revision: {sha2}",
                    ],
                    cwd=seed,
                    check=True,
                )
                subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)

                # Create legacy superseded branch + orphaned branch on origin
                for legacy_ref in (f"promote/runa/{sha0}", "promote/runa/orphaned-closed"):
                    subprocess.run(["git", "checkout", "--quiet", "-b", legacy_ref, "origin/main"], cwd=seed, check=True)
                    (seed / prod_rel).write_text(render_img("9"), encoding="utf-8")
                    subprocess.run(["git", "commit", "-am", "legacy", "--quiet"], cwd=seed, check=True)
                    subprocess.run(["git", "push", "--quiet", "origin", legacy_ref], cwd=seed, check=True)
                subprocess.run(["git", "checkout", "--quiet", "main"], cwd=seed, check=True)

                # Verify staging monotonic guard blocks older sha1, but allows explicit override
                stg_out = root / "stg_out"
                stg_env = os.environ.copy()
                stg_env.update(
                    {
                        "T14_ENABLED": "false",
                        "EXPLICIT_SOURCE_REVISION": "false",
                        "APPLICATION": "runa",
                        "SOURCE_SHA": sha1,
                        "SOURCE_REPO": "Shhinobi/Runa",
                        "GITOPS_REPO": "Shhinobi/k3s-gitops-prod",
                        "BASE_BRANCH": "main",
                        "STAGING_FILE": stg_rel,
                        "GITOPS_TOKEN": "gtok",
                        "APP_READ_TOKEN": "tok",
                        "API_URL": api_url,
                        "GITHUB_OUTPUT": str(stg_out),
                    }
                )
                res = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", stg_guard_step["run"]],
                    cwd=seed,
                    env=stg_env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("Skipping out-of-order staging promotion", res.stdout)
                self.assertEqual(stg_out.read_text(encoding="utf-8").strip(), "superseded=true")

                stg_out.unlink()
                stg_env["EXPLICIT_SOURCE_REVISION"] = "true"
                subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", stg_guard_step["run"]],
                    cwd=seed,
                    env=stg_env,
                    check=True,
                )
                self.assertEqual(stg_out.read_text(encoding="utf-8").strip(), "superseded=false")

                mock_state_path = root / "gh_state.json"
                state_holder["path"] = mock_state_path
                mock_state_path.write_text(
                    json.dumps(
                        {
                            "next_pr": 132,
                            "create_calls": 0,
                            "update_calls": 0,
                            "comments": [],
                            "prs": [
                                {
                                    "number": 118,
                                    "state": "open",
                                    "title": f"chore(runa): promote {sha0[:12]} to production",
                                    "body": "legacy",
                                    "head": {"ref": f"promote/runa/{sha0}"},
                                    "base": {"ref": "main"},
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                def run_pr_cycle(source_sha: str, digest_char: str) -> dict[str, str]:
                    work = root / f"work-{source_sha[:6]}-{digest_char}-{os.urandom(2).hex()}"
                    subprocess.run(["git", "clone", "--quiet", str(origin), str(work / "gitops")], check=True)
                    rel_dir = work / "release"
                    rel_dir.mkdir()
                    (rel_dir / "release-api.json").write_text(
                        json.dumps(
                            {
                                "component": "api",
                                "image": "registry.lan.kyo.ninja/runa/api",
                                "digest": f"sha256:{digest_char * 64}",
                                "workload": "api",
                                "container": "api",
                            }
                        ),
                        encoding="utf-8",
                    )
                    baseline_json = runner_temp / "prod-baseline.json"
                    base_env = os.environ.copy()
                    base_env.update(
                        {
                            "APPLICATION": "runa",
                            "SOURCE_REPO": "Shhinobi/Runa",
                            "PROD_FILE": prod_rel,
                            "IMAGE_PROMOTION": "kustomize-images",
                            "BASELINE_JSON": str(baseline_json),
                            "PYTHONPATH": str(root / "no_yaml_shim"),
                        }
                    )
                    shim_dir = root / "no_yaml_shim"
                    shim_dir.mkdir(exist_ok=True)
                    (shim_dir / "yaml.py").write_text("raise ImportError('no PyYAML on slim runner')\n", encoding="utf-8")
                    subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", baseline_step["run"]],
                        cwd=work / "gitops",
                        env=base_env,
                        check=True,
                    )
                    subprocess.run(
                        [
                            "python3",
                            str(ROOT / "scripts/update_gitops_images.py"),
                            str(work / "gitops" / prod_rel),
                            str(rel_dir),
                        ],
                        check=True,
                    )
                    gh_output = work / "gh_output"
                    gh_output.write_text("", encoding="utf-8")
                    branch_env = os.environ.copy()
                    branch_env.update(
                        {
                            "APPLICATION": "runa",
                            "SOURCE_SHA": source_sha,
                            "SOURCE_REPO": "Shhinobi/Runa",
                            "GITOPS_REPO": "JustShinobi/k3s-gitops-prod",
                            "BASE_BRANCH": "main",
                            "PROD_FILE": prod_rel,
                            "EXPLICIT_SOURCE_REVISION": "false",
                            "GITOPS_TOKEN": "gtok",
                            "APP_READ_TOKEN": "tok",
                            "API_URL": api_url,
                            "RUNNER_TEMP": str(runner_temp),
                            "GITHUB_OUTPUT": str(gh_output),
                        }
                    )
                    subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", branch_step["run"]],
                        cwd=work / "gitops",
                        env=branch_env,
                        check=True,
                    )
                    outputs = dict(
                        line.split("=", 1)
                        for line in gh_output.read_text(encoding="utf-8").splitlines()
                        if "=" in line
                    )
                    if outputs.get("superseded") != "true":
                        js_harness = f"""
const fs = require('fs');
const {{ execFileSync }} = require('child_process');
const statePath = process.env.MOCK_STATE_PATH;
const originDir = process.env.MOCK_ORIGIN_DIR;
const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
const core = {{ info: () => {{}}, warning: () => {{}} }};
const context = {{ repo: {{ owner: 'Shhinobi', repo: 'Runa' }}, serverUrl: 'https://github.com', runId: 999 }};
const github = {{
  paginate: async (fn, opts) => (await fn(opts)).data,
  rest: {{
    repos: {{
      get: async () => ({{ data: {{ owner: {{ login: 'Shhinobi' }}, name: 'k3s-gitops-prod' }} }})
    }},
    pulls: {{
      list: async ({{ state: prState }}) => ({{
        data: state.prs.filter(p => !prState || p.state === prState)
      }}),
      create: async ({{ head, base, title, body }}) => {{
        state.create_calls += 1;
        const pr = {{ number: state.next_pr++, state: 'open', title, body, head: {{ ref: head }}, base: {{ ref: base }} }};
        state.prs.push(pr);
        return {{ data: pr }};
      }},
      update: async ({{ pull_number, title, body, state: nextState }}) => {{
        const pr = state.prs.find(p => p.number === pull_number);
        if (title !== undefined) {{ pr.title = title; state.update_calls += 1; }}
        if (body !== undefined) pr.body = body;
        if (nextState !== undefined) pr.state = nextState;
        return {{ data: pr }};
      }}
    }},
    issues: {{
      createComment: async ({{ issue_number, body }}) => {{
        state.comments.push({{ issue_number, body }});
        return {{ data: {{}} }};
      }}
    }},
    git: {{
      listMatchingRefs: async ({{ ref }}) => {{
        const out = execFileSync('git', ['--git-dir', originDir, 'for-each-ref', '--format=%(refname)', `refs/${{ref}}`], {{ encoding: 'utf8' }});
        return {{ data: out.split(/\\r?\\n/).filter(Boolean).map(r => ({{ ref: r }})) }};
      }},
      deleteRef: async ({{ ref }}) => {{
        execFileSync('git', ['--git-dir', originDir, 'update-ref', '-d', `refs/${{ref}}`]);
        return {{}};
      }}
    }}
  }}
}};
(async () => {{
{pr_step['with']['script']}
  fs.writeFileSync(statePath, JSON.stringify(state, null, 2));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
                        pr_env = os.environ.copy()
                        pr_env.update(
                            {
                                "MOCK_STATE_PATH": str(mock_state_path),
                                "MOCK_ORIGIN_DIR": str(origin),
                                "REPOSITORY": "JustShinobi/k3s-gitops-prod",
                                "HEAD_BRANCH": outputs["branch"],
                                "BASE_BRANCH": "main",
                                "APPLICATION": "runa",
                                "SOURCE_SHA": source_sha,
                                "PROMOTION_MODE": "staging-and-production",
                                "IMAGE_PROMOTION": "kustomize-images",
                                "PRODUCTION_PATH": "clusters/prod/workloads/runa-prd",
                                "PRODUCTION_APP": "prd-runa",
                                "STAGING_APP": "stg-runa",
                                "HAS_CHANGES": outputs["has_changes"],
                                "UNCHANGED_DIGESTS": outputs["unchanged_digests"],
                                "BASELINE_FILE": str(baseline_json),
                                "RELEASE_DIR": str(rel_dir),
                                "APP_READ_TOKEN": "tok",
                                "GITHUB_API_URL": api_url,
                            }
                        )
                        subprocess.run(["node", "-e", js_harness], cwd=work, env=pr_env, check=True)
                    return outputs

                # 1. First release (sha1, digest 'a'): creates canonical PR #132, closes #118, deletes legacy + orphan refs
                out1 = run_pr_cycle(sha1, "a")
                self.assertEqual(out1["branch"], "promote/runa/production")
                self.assertEqual(out1["pushed"], "true")
                state = json.loads(mock_state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["create_calls"], 1)
                self.assertEqual([p["number"] for p in state["prs"] if p["state"] == "open"], [132])
                refs_after_1 = subprocess.check_output(
                    ["git", "--git-dir", str(origin), "for-each-ref", "--format=%(refname)", "refs/heads/promote/"],
                    text=True,
                ).splitlines()
                self.assertEqual(refs_after_1, ["refs/heads/promote/runa/production"])

                # Advance origin/main with an unrelated staging commit to ensure rolling branch rebases cleanly onto 1 commit
                subprocess.run(["git", "pull", "--quiet", "--rebase", "origin", "main"], cwd=seed, check=True)
                (seed / stg_rel).write_text(render_img("b"), encoding="utf-8")
                subprocess.run(["git", "commit", "-am", "stg advance", "--quiet"], cwd=seed, check=True)
                subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)

                # 2. Second release (sha2, digest 'b'): updates PR #132 IN-PLACE (zero new PRs!), keeps 1 clean commit on origin/main
                out2 = run_pr_cycle(sha2, "b")
                self.assertEqual(out2["pushed"], "true")
                state = json.loads(mock_state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["create_calls"], 1, "must NOT create a second PR!")
                self.assertEqual(state["update_calls"], 1, "must update existing PR #132 in-place!")
                open_pr = next(p for p in state["prs"] if p["state"] == "open")
                self.assertEqual(open_pr["number"], 132)
                self.assertEqual(open_pr["title"], f"chore(runa): promote {sha2[:12]} to production")
                self.assertIn("Commits Since Current Production (2)", open_pr["body"])
                self.assertIn("`sha256:000000000000…`", open_pr["body"])
                self.assertIn("🔄 Updated", open_pr["body"])
                ahead_count = subprocess.check_output(
                    ["git", "--git-dir", str(origin), "rev-list", "--count", "refs/heads/main..refs/heads/promote/runa/production"],
                    text=True,
                ).strip()
                self.assertEqual(ahead_count, "1")
                branch_oid_after_2 = subprocess.check_output(
                    ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/promote/runa/production"],
                    text=True,
                ).strip()

                # 3. Third release (sha3, IDENTICAL digest 'b' — CI/docs-only commit): skips push (preserves OID), updates PR metadata!
                out3 = run_pr_cycle(sha3, "b")
                self.assertEqual(out3["pushed"], "false")
                self.assertEqual(out3["unchanged_digests"], "true")
                branch_oid_after_3 = subprocess.check_output(
                    ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/promote/runa/production"],
                    text=True,
                ).strip()
                self.assertEqual(branch_oid_after_3, branch_oid_after_2, "branch OID must stay untouched when digests are identical")
                state = json.loads(mock_state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["create_calls"], 1)
                open_pr = next(p for p in state["prs"] if p["state"] == "open")
                self.assertEqual(open_pr["title"], f"chore(runa): promote {sha3[:12]} to production")
                self.assertIn("Runtime container digests are identical", open_pr["body"])
                self.assertIn("Commits Since Current Production (4)", open_pr["body"])

                # 4. Out-of-order stale builds:
                # 4a: sha2_5 (between sha2 and sha3, with DISTINCT digest 'e') MUST be blocked via PR body watermark (zero split-brain!)
                out4_mid = run_pr_cycle(sha2_5, "e")
                self.assertEqual(out4_mid["superseded"], "true")
                self.assertEqual(out4_mid["pushed"], "false")
                branch_oid_after_4_mid = subprocess.check_output(
                    ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/promote/runa/production"],
                    text=True,
                ).strip()
                self.assertEqual(branch_oid_after_4_mid, branch_oid_after_2, "delayed sha2_5 must NOT overwrite branch!")

                # 4b: sha2 (identical digest) and sha1 (older digest) are ALSO blocked monotonically
                out4a = run_pr_cycle(sha2, "b")
                self.assertEqual(out4a["superseded"], "true")
                self.assertEqual(out4a["pushed"], "false")

                out4 = run_pr_cycle(sha1, "a")
                self.assertEqual(out4["superseded"], "true")
                self.assertEqual(out4["pushed"], "false")
                state = json.loads(mock_state_path.read_text(encoding="utf-8"))
                open_pr = next(p for p in state["prs"] if p["state"] == "open")
                self.assertEqual(open_pr["title"], f"chore(runa): promote {sha3[:12]} to production")

                # 5. Production catches up on main (recording Source-Revision: sha3) -> re-running sha3 closes PR #132 and deletes branch!
                subprocess.run(["git", "pull", "--quiet", "--rebase", "origin", "main"], cwd=seed, check=True)
                (seed / prod_rel).write_text(render_img("b"), encoding="utf-8")
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "-am",
                        f"chore(runa): promote {sha3[:12]} to production",
                        "-m",
                        "Source-Repository: Shhinobi/Runa",
                        "-m",
                        f"Source-Revision: {sha3}",
                        "--quiet",
                    ],
                    cwd=seed,
                    check=True,
                )
                subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)

                out5 = run_pr_cycle(sha3, "b")
                self.assertEqual(out5["has_changes"], "false")
                state = json.loads(mock_state_path.read_text(encoding="utf-8"))
                self.assertEqual([p for p in state["prs"] if p["state"] == "open"], [])
                refs_after_5 = subprocess.check_output(
                    ["git", "--git-dir", str(origin), "for-each-ref", "--format=%(refname)", "refs/heads/promote/"],
                    text=True,
                ).splitlines()
                self.assertEqual(refs_after_5, [])

                # 6. Post-merge delete_branch_on_merge guard: promote/runa/production no longer exists,
                #    and a delayed sha1 (digest 'a' != prod 'b') MUST still be blocked by Watermark 1 on origin/main!
                out6 = run_pr_cycle(sha1, "a")
                self.assertEqual(out6["superseded"], "true")
                self.assertEqual(out6["pushed"], "false")
                state = json.loads(mock_state_path.read_text(encoding="utf-8"))
                self.assertEqual([p for p in state["prs"] if p["state"] == "open"], [])
        finally:
            server.shutdown()
            server.server_close()

    def test_rollback_reuses_rolling_branch_and_supports_chart_values(self) -> None:
        import yaml

        rollback_text = (ROOT / ".github/workflows/rollback.yml").read_text(encoding="utf-8")
        self.assertIn('branch="rollback/${APPLICATION}/production"', rollback_text)
        self.assertIn('--force-with-lease="refs/heads/${branch}:${remote_oid}"', rollback_text)
        self.assertIn("values-prd.yaml", rollback_text)

        wf = yaml.safe_load(rollback_text)
        steps = wf["jobs"]["rollback"]["steps"]
        select_step = next(s for s in steps if s.get("name") == "Select bounded target")
        restore_step = next(s for s in steps if s.get("name") == "Restore exact image patch")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out_file = root / "select_out"
            env = os.environ.copy()
            env.update(
                {
                    "TARGET_ENVIRONMENT": "prod",
                    "BASE_BRANCH": "main",
                    "STAGING_PATH": "clusters/prod/workloads/app-stg",
                    "PRODUCTION_PATH": "clusters/prod/workloads/app-prd",
                    "IMAGE_PROMOTION": "chart-values",
                    "GITHUB_OUTPUT": str(out_file),
                }
            )
            subprocess.run(["bash", "-euo", "pipefail", "-c", select_step["run"]], env=env, check=True)
            kv = dict(line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines())
            self.assertEqual(kv["file"], "values-prd.yaml")

            origin = root / "origin.git"
            work = root / "gitops"
            runner_temp = root / "runner_temp"
            runner_temp.mkdir()
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)], check=True)
            subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=work, check=True)

            target_rel = Path("clusters/prod/workloads/app-prd/values-prd.yaml")
            (work / target_rel).parent.mkdir(parents=True)
            (work / target_rel).write_text("api:\n  image:\n    digest: sha256:" + "1" * 64 + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=work, check=True)
            subprocess.run(["git", "commit", "-m", "rev1", "--quiet"], cwd=work, check=True)
            rev1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()

            (work / target_rel).write_text("api:\n  image:\n    digest: sha256:" + "2" * 64 + "\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "rev2", "--quiet"], cwd=work, check=True)
            rev2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()

            (work / target_rel).write_text("api:\n  image:\n    digest: sha256:" + "3" * 64 + "\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "rev3", "--quiet"], cwd=work, check=True)
            subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=work, check=True)

            for rev in (rev1, rev2):
                r_out = root / f"restore_{rev[:6]}"
                r_env = os.environ.copy()
                r_env.update(
                    {
                        "REVISION": rev,
                        "TARGET_PATH": "clusters/prod/workloads/app-prd",
                        "TARGET_FILE": "values-prd.yaml",
                        "TARGET_ENVIRONMENT": "prod",
                        "APPLICATION": "app",
                        "BASE_BRANCH": "main",
                        "RUNNER_TEMP": str(runner_temp),
                        "GITHUB_OUTPUT": str(r_out),
                    }
                )
                subprocess.run(["bash", "-euo", "pipefail", "-c", restore_step["run"]], cwd=work, env=r_env, check=True)
                r_kv = dict(line.split("=", 1) for line in r_out.read_text(encoding="utf-8").splitlines())
                self.assertEqual(r_kv["branch"], "rollback/app/production")
                self.assertEqual(r_kv["has_changes"], "true")

            ahead = subprocess.check_output(
                ["git", "--git-dir", str(origin), "rev-list", "--count", "refs/heads/main..refs/heads/rollback/app/production"],
                text=True,
            ).strip()
            self.assertEqual(ahead, "1")

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
