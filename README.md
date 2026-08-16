# platform-workflows

Reusable CI/CD contracts for private homelab applications. The pipeline builds once on an
internal self-hosted runner, promotes the resulting immutable digests to staging, waits for
Argo CD health, runs the application smoke contract and opens a production GitOps pull request.

## Delivery flow

1. Pull requests call `verify.yml`; validation and the fixed `scripts/ci/verify` contract run on
   the `arc-k3s` scale set.
2. Trusted pushes call `application-release.yml`. `executor: arc` uses ARC; `executor: jit` uses
   the existing `self-hosted`, `proxmox-lxc`, `crossbuild` labels for controlled comparison.
3. Every component is built and pushed to internal Zot. BuildKit provenance, an SPDX SBOM and a
   blocking Trivy HIGH/CRITICAL scan are retained with the run.
4. Exact image digests update staging `images.yaml` on validated `main`; the GitOps channel
   workflow fast-forwards `deploy/stg`.
5. The workflow waits for the configured Argo CD Application revision to be `Synced/Healthy`,
   then runs the fixed `scripts/ci/smoke-stg` contract.
6. The same digests are written to production `images.yaml` on a pull request. Merging that PR is
   the human production gate and lets GitOps fast-forward `deploy/prod`.

No image is rebuilt between environments. Zot publication is never scheduled on a GitHub-hosted
runner.

## Consumer contract

Copy [`examples/application-ci.yml`](examples/application-ci.yml) to
`.github/workflows/application-ci.yml`, copy [`examples/rollback.yml`](examples/rollback.yml), and
create `.ci/application.yaml` based on [`examples/application.yaml`](examples/application.yaml).
Replace `REPLACE_WITH_40_CHARACTER_SHA` with an immutable commit from this repository.

The repository must provide two executable, input-free entrypoints:

- `scripts/ci/verify`: lint, unit tests, integration tests and build checks;
- `scripts/ci/smoke-stg`: tests the already reconciled staging endpoint.

The `staging` GitHub Environment must define the non-secret variable `STAGING_BASE_URL`. It is the
only runtime input passed to the smoke script; applications derive any component-specific health
URLs from that base URL inside their reviewed wrapper.

Commands cannot be supplied through workflow inputs or the descriptor. Both scripts are reviewed
application source, so the workflow never evaluates descriptor content as shell code.

### Descriptor fields

`schemaVersion` is currently `1`. Each component defines:

- `name`: stable release component name;
- `image`: Zot repository without tag or digest; repositories may be shared by components;
- `context`, `dockerfile`, optional `target`, and optional `platforms` (`linux/amd64` or
  `linux/arm64`);
- `workload` and `container`: exact Deployment/container patched in the environment `images.yaml`;
- `rolloutProfile`: `deployment`, `bluegreen`, or `canary` metadata for platform tooling.

`gitops` defines the `owner/repository`, validated source branch (`baseBranch`), release channels
(`stagingBranch` and `productionBranch`), environment directories and Argo CD Application names.
`stagingPath/images.yaml` and `productionPath/images.yaml` must be multi-document
strategic-merge patches containing every declared Deployment/container target.

The complete machine-readable contract is in
[`contracts/application.schema.json`](contracts/application.schema.json). Validate locally with:

```bash
python3 -m pip install --requirement requirements-dev.txt
python3 scripts/validate_descriptor.py .ci/application.yaml
```

## Required configuration

Application repositories expose these Actions secrets to the reusable release workflow:

| Secret | Scope |
|---|---|
| `PLATFORM_GITOPS_TOKEN` | Fine-grained PAT allowed to write the GitOps repository and open PRs |
| `ARGOCD_SERVER` / `ARGOCD_AUTH_TOKEN` | Read-only Argo CD API access to application status |

ARC mounts `arc-runners/arc-zot-docker-config` read-only at
`/run/secrets/zot/config.json`. The release workflow copies it with mode `0600` to a writable,
job-scoped `DOCKER_CONFIG`; Zot credentials are deliberately not copied into application
repository secrets. JIT runners must provide the same read-only source file before they are
selected with `executor: jit`.

Create GitHub environments named `staging`, `production-promotion`, `staging-rollback`, and
`production-rollback`. Approval is normally required only for production rollback; the promotion
PR remains the normal production approval gate.

The private repository setting **Actions > General > Access** must allow access from repositories
owned by `JustShinobi`. Publish the composite actions under the stable `v1` tag before onboarding a
consumer. Consumer reusable workflow calls should still be pinned to a full commit SHA.

## Safety properties

- Workflow-level `contents: read`; cross-repository writes use only the explicit fine-grained
  `PLATFORM_GITOPS_TOKEN` secret.
- Descriptor keys, paths, refs, image names, platforms and rollout profiles are allowlisted.
- Build runner labels are hardcoded; callers choose only `arc` or `jit`.
- Builds run up to 3 components in parallel per release matrix on ARC runners.
- Every job has a timeout and promotion jobs use concurrency locks.
- Rollback accepts only a full Git commit SHA. Staging rollback commits to validated `main` and is
  advanced by the GitOps channel workflow; production rollback opens a PR.

## Repository validation

```bash
python3 -m unittest discover -s tests -v
python3 scripts/validate_descriptor.py examples/application.yaml --no-check-files --print-matrix
```

`self-test.yml` runs the same checks for this repository. Its jobs do not contact Zot or the
clusters.
