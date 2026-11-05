# Deployment strategy

This document describes the intended reproducible release and managed enterprise deployment process for `cli-agent`. It is organization-neutral: the open-source project supplies technical artifacts and security mechanisms; each organization provides its own policy, approvals and distribution infrastructure.

Treat this as a reference architecture. An MSI is the preferred Windows approach for a managed rollout, but is not mandatory for every installation.

## Target architecture

The intended process separates upstream releases, organization-specific packaging and end-user execution:

```text
Source / Git tag
        |
        v
Release CI
  - Tests
  - Locked dependencies
  - Wheel + sdist
  - CycloneDX SBOM
  - SHA-256 checksums
        |
        v
Published upstream release
  - Python Package Index / PyPI
  - GitHub Release with additional assets
        |
        v
Organization-specific packaging
  - Approved cli-agent version
  - Verified Python runtime prerequisite
  - Pinned Python dependencies
  - Organization-specific admin_config.toml
  - Optional approved MCP extensions
        |
        v
MSI or equivalent managed software package
        |
        v
Software distribution
  - Software Center / Intune / SCCM / equivalent
        |
        v
Workstation
  - Installation/update with admin or SYSTEM privileges
  - Execution of cli-agent as an ordinary user
```

## Current implementation status

After a successful test matrix, GitHub Actions already produces a wheel, a source distribution, a CycloneDX SBOM for Windows/Python 3.11, and SHA-256 checksums. Files are currently made available as GitHub Actions artifacts with a 30-day retention period. See [ci-release.md](ci-release.md).

A permanent GitHub Release and tag-based public publishing workflow have not yet been implemented. The following sections therefore distinguish the existing technical build process from the intended subsequent release and deployment stages.

## 1. Upstream releases

An approved release should be built from an unambiguous source revision, typically a Git tag.

Release CI should at least:

1. Verify lockfile consistency.
2. Run the test suite in the locked environment.
3. Build wheel and source distribution.
4. Test-install or validate the resulting Python artifacts.
5. Create a CycloneDX SBOM.
6. Publish cryptographic checksums for the release artifacts.

The wheel is the primary installable Python artifact. The source distribution is an additional standards-compliant source package.

For public releases, wheel and sdist can be published through PyPI; the SBOM and other metadata can be attached as versioned GitHub Release assets.

## 2. SBOM and resolved dependencies

The SBOM should describe actual resolved, tested runtime dependencies, not merely version ranges from `pyproject.toml`. The reference is an environment built from `uv.lock`.

An upstream SBOM always represents a concrete reference environment, such as Windows/Python 3.11. Platform markers may yield different dependency graphs on other operating systems or Python versions.

For a specific enterprise deployment, verify whether the upstream SBOM fully captures the deployed environment. If the enterprise package includes a different Python runtime, additional packages or MCP components, generate a complete SBOM or SCA assessment for **that exact package**.

## 3. Organization-specific policy

The security architecture deliberately separates user configuration from machine-wide `admin_config.toml`.

Upstream stays generic. The deploying organization defines its policy, including:

- Permitted model hosts.
- Permitted HTTP MCP hosts.
- Permitted web destinations.
- Individually approved external stdio MCP launch profiles.
- Optionally trusted MCP instructions.
- Optional permanent tool auto-approvals bound to pinned contracts.
- Approved credential sources.

Organization-specific `admin_config.toml` does not belong in the public upstream repository. Maintain and version it as part of an internal deployment package or a separate policy package.

Managed deployment must preserve the boundary between machine-wide administrator policy and user-controlled configuration.

### OKF knowledge roots

The optional OKF root intentionally does **not** belong in machine-wide `admin_config.toml`. `[okf].repository` is part of user/project configuration and may reside outside the project workspace.

After selection, OKF tools are deterministically constrained to the chosen root and are read-only. However, the application currently provides no administrative `okf_allowed_roots` allowlist.

Even in managed deployment, selecting the root remains the user's decision. It is treated like other explicitly supplied LLM context: users select a local knowledge source whose relevant content may be passed to their configured LLM. A central allowed-roots list is therefore not part of the intended deployment model.

The core agent technically guarantees containment **within the user-selected root**. Once selected, the Knowledge run cannot escape it and receives only the specified read-only OKF tools.

## 4. Windows reference deployment

For managed Windows workstations, an MSI or equivalent internal package is the reference approach.

### Python prerequisite

Prefer an existing company-managed Python distribution instead of embedding a second runtime into `cli-agent`.

The deployment package should verify the supported Python version explicitly. Install the application and its dependencies in an isolated application-specific environment, not globally into the centrally managed Python installation.

### No dependency resolution on workstations

Production MSI installation should **not** blindly execute an unconstrained command against PyPI or another index:

```text
pip install cli-agent
```

Instead, build the deployment from the already verified release and include exact approved Python package versions or obtain them from a controlled internal repository.

The resulting provenance chain is:

```text
Verified release
    -> Pinned dependency versions
    -> SBOM / SCA
    -> Deployment package
    -> Workstation
```

### Installation paths

A possible Windows layout is:

```text
C:\Program Files\<Organization>\cli-agent\
    Application / isolated Python environment

C:\ProgramData\cli-agent\
    admin_config.toml
```

Organizations may use other paths provided security boundaries remain intact.

### Privileges

Installation, upgrades and machine-wide policy changes should use administrator or SYSTEM privileges.

Normal `cli-agent` execution should use an ordinary user account.

```text
Installation / update / policy change -> Admin or SYSTEM
Execute cli-agent                   -> Ordinary user
Project and user configuration      -> Ordinary user
Change admin_config.toml            -> Admin or SYSTEM
```

On Windows, protect `admin_config.toml` so ordinary users can read it but cannot modify or replace it.

## 5. Managed software distribution

Deploy finished MSI/packages through the organization's established software distribution tooling, such as Software Center, Microsoft Intune, Configuration Manager or an equivalent.

Developers then do not need to perform their own privileged installation steps. The distribution service may install under SYSTEM/administrator privileges while `cli-agent` runs as a normal user afterward.

A managed rollout should support version control, staged deployment, updates and rollback.

## 6. Updates and rollback

Do not upgrade existing installations to a new `cli-agent` version without evaluation.

The intended pipeline is:

```text
New upstream release
    -> CI and tests
    -> Updated SBOM / dependency analysis
    -> Security and operations assessment
    -> New managed software package
    -> Pilot group
    -> Broad rollout
```

Rollback must at least support:

- Reverting to the previous approved application version.
- Restoring a prior administrator policy.
- Disabling optional MCP extensions.
- Rotating credentials or tokens when needed.

Version application and policy separately. An organization should not need to rebuild `cli-agent` merely because a permitted internal host changes.

## 7. Optional MCP extensions

External MCP packages are **not automatically covered** by core-agent approval.

In particular, components with Docker daemon access, code execution or similar host-level capabilities require separate evaluation. If deployed alongside `cli-agent`, include them in the concrete deployment package's SBOM/SCA assessment.

External stdio MCP launch configuration must remain administratively bound in `admin_config.toml`. Ordinary users cannot replace a trusted external server's command, arguments or environment through project configuration.

## 8. Other platforms

MSI is the Windows reference packaging mechanism, not the security boundary itself.

Linux and other platforms can implement the same model with their normal managed package tooling, such as DEB/RPM or organization-specific packaging.

Regardless of package format, essential requirements remain:

- Reproducible, traceable release revision.
- Documented components and dependencies.
- Organization-controlled administrator policy.
- No relaxation of that policy by user configuration.
- Installation/policy changes beyond ordinary user privileges.
- Normal execution without administrator privileges.
- Controlled updates and rollback.

## 9. Local developer setup versus managed deployment

Local installation from a Git checkout or via `pipx` is appropriate for development, tests and individual use.

It is **not equivalent** to an approved enterprise deployment. Managed rollout adds reproducible packaging, organization policy, software distribution and an approval process to upstream release artifacts.

See [company-deployment-checklist.md](company-deployment-checklist.md) for operating approvals and [security.md](security.md) for agent security boundaries.
