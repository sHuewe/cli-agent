# CI, package builds, SBOM and publishing

The GitHub Actions pipeline tests `cli-agent` and, after a successful test matrix, builds installable Python artifacts and a CycloneDX SBOM.

## Test matrix

Tests run on Windows and Ubuntu using Python 3.11. Before tests, CI verifies that `uv.lock` is up to date. The test environment is then created exactly from that lockfile:

```text
uv sync --frozen --extra dev
```

## Package build

After the test matrix passes, the Windows job `Build distributions and SBOM`:

1. Checks `uv.lock` again.
2. Creates a runtime environment without development extras via `uv sync --frozen`.
3. Builds wheel and source distribution with `uv build --no-sources`.
4. Test-installs the produced wheel.
5. Additionally installs the wheel in isolation with optional `web` extras and verifies startup of Web UI dependencies.
6. Confirms that a source distribution was produced.
7. Creates a CycloneDX SBOM from the locked runtime environment.
8. Generates SHA-256 checksums.
9. Uploads these files as a GitHub Actions artifact.

The build backend `hatchling` is pinned to an exact version in `pyproject.toml`. This prevents the build environment from resolving a different compatible version on every run. `uv build --no-sources` prevents local `tool.uv.sources` overrides from silently entering a releasable build.

## SBOM

The SBOM uses a pinned `cyclonedx-bom` version to generate CycloneDX JSON. The generator executes as a separate `uv tool`, not as a `cli-agent` runtime dependency.

It uses the runtime environment previously created with `uv sync --frozen`, describing the actually resolved Python packages for the Windows / Python 3.11 reference environment, not merely version ranges from `pyproject.toml`.

Filename:

```text
dist/cli-agent-python311-windows.cdx.json
```

The platform-specific name is intentional: environment markers can produce different dependency graphs across operating systems and Python versions.

The build also creates:

```text
dist/SHA256SUMS.txt
```

Checksums cover wheel, source distribution and SBOM.

## Artifact location

`actions/upload-artifact` stores files in the corresponding GitHub Actions workflow run:

```text
Actions
  -> workflow run
  -> Artifacts
```

The artifact name includes the commit SHA:

```text
cli-agent-package-<commit-sha>
```

Retention is currently 30 days. An Actions artifact is therefore neither a permanent release nor a Python package repository.

A future public release process may publish the same verified files permanently as GitHub Release assets, while uploading wheel and source distribution to a Python Package Index.

## Manual publishing

The workflow may be started manually through `workflow_dispatch`, with a boolean `publish` parameter defaulting to `false`.

The publish job executes only when `publish = true`, the workflow was explicitly started from the `main` branch, and test/package jobs passed. A manual run from a feature branch or tag cannot publish production packages.

Publishing additionally uses the GitHub Environment `package-publish`. For production it should be protected in repository settings, e.g. with required reviewers and restrictions to `main`.

The job downloads exactly the Python distributions built in the same workflow and uploads wheel and source distribution using `uv publish`.

Two repository variables are required:

```text
PYTHON_PUBLISH_URL
PYTHON_PUBLISH_CHECK_URL
```

`PYTHON_PUBLISH_URL` is the package repository upload URL. `PYTHON_PUBLISH_CHECK_URL` is the corresponding index/check URL. `uv publish --check-url` can use it to identify already existing distribution artifacts before upload, allowing a restarted partially successful run to skip uploaded files instead of failing on duplicates.

Authentication supports exactly one of the following alternatives:

```text
Token:
PYTHON_PUBLISH_TOKEN

or username/password:
PYTHON_PUBLISH_USERNAME
PYTHON_PUBLISH_PASSWORD
```

Token authentication and username/password must **not** both be configured. The workflow enforces this before upload and exports only the selected credentials to `uv publish`.

Normal pushes and pull requests do not publish packages.

## Relationship to the planned release process

Current CI reproducibly builds and checks release candidates, but stores them only as short-lived Actions artifacts. A permanent GitHub Release, tag-based publishing, PyPI Trusted Publishing, signing and automated vulnerability scanning are not yet part of this workflow.

The overall deployment strategy is documented in [deployment.md](deployment.md). A future release chain could follow:

```text
Git tag
  -> Tests
  -> Wheel + sdist
  -> CycloneDX SBOM
  -> Checksums
  -> Vulnerability scan / approval
  -> GitHub Release
  -> Publish to Python Package Index
  -> optional organization-specific MSI / software package
```
