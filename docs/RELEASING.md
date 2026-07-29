# Releasing Rustwright

This guide is for the release owner.

## Agent-assisted release

Use `/version-upgrade <version> prepare` to create and validate a release PR
without publishing. Use `/version-upgrade <version> full release` only when the
agent should merge the prepared release, run final dry runs on the merged
commit, tag it, and publish to both PyPI and npm. The skill is defined in
`.claude/skills/version-upgrade/SKILL.md`.

## One-time setup

- [ ] Confirm the `rustwright` name is still available on both PyPI and npm. A name is not reserved until the first successful publish.
- [ ] In GitHub, open **Settings → Environments → New environment**, create `pypi`, and add a required reviewer.
- [ ] In PyPI, open **Account settings → Publishing → Add a new pending publisher** and enter exactly:
  - PyPI project name: `rustwright`
  - Owner: `Skyvern-AI`
  - Repository: `rustwright`
  - Workflow: `release-pypi.yml`
  - Environment: `pypi`
- [ ] Do not create a PyPI API token. `.github/workflows/release-pypi.yml` uses the `pypi` GitHub environment and OIDC Trusted Publishing.
- [ ] In GitHub, create an `npm` environment, add a required reviewer, and add an environment secret named `NPM_TOKEN`.
- [ ] Supply `NPM_TOKEN`: create an npm granular access token with **Packages and scopes: Read and write**, **All Packages** for the first unscoped publish, and **Bypass 2FA** for non-interactive publishing. Set an expiration and calendar a rotation. After the first release, replace it with a token restricted to `rustwright` if npm permits that scope.
- [ ] Confirm the npm account behind `NPM_TOKEN` may create the unscoped public package `rustwright`. Unscoped packages are owned by npm user accounts, not organizations.
- [ ] `examples/quickstart.py` is the public smoke test used by the registry-verification step below; confirm it still runs cleanly before tagging.

`rustwright-core` and `rustwright` are already published on crates.io, but no workflow publishes them: there is no crates.io job and no `CARGO_REGISTRY_TOKEN`, so a `v*` tag leaves both crates untouched and they are updated by hand. Publishing the core commits the team to Rust API compatibility, documentation, security advisories, and an additional release channel, so decide deliberately whether to keep that channel current, add a dedicated crates.io workflow, or yank it — but do not let it drift silently behind the tagged releases.

## Prepare a release

- [ ] Choose one version in SemVer form, for example `0.2.0`. A single `v*` tag drives the PyPI, npm, NuGet, RubyGems, and Maven Central workflows, and each one compares its own packages against that tag, so every version field in the tree has to hold that exact string.
- [ ] Set that exact string in every source-of-truth field. All five tagged
      workflows validate their own packages, so a field missed here fails the
      release at tag time, after the tag is already pushed:
  - `pyproject.toml` → `[project].version`
  - `Cargo.toml` → `[package].version` for `rustwright-core`
  - `capi/Cargo.toml` → `[package].version` for `rustwright-capi`
  - `rust-native/Cargo.toml` → `[package].version` for `rustwright`
  - `node/Cargo.toml` → `[package].version` for `rustwright-node`
  - `node/package.json` → `version`
  - `csharp/Rustwright/Rustwright.csproj` → `<Version>`
  - `ruby/lib/rustwright.rb` → `VERSION` (`ruby/rustwright.gemspec` reads it, so
    the gemspec itself needs no edit)
  - `java/build.gradle.kts` → **two** sites: the top-level `version =` and the
    literal inside the `coordinates(...)` call. The Maven validator only checks
    the first, so a missed `coordinates(...)` bump publishes under the wrong
    version instead of failing.
- [ ] Set the same string in the shipped runtime metadata:
  - `python/rustwright/sync_api.py` → Rustwright creator `version` in `_write_har`
  - `python/rustwright/sync_api.py` → `playwrightVersion` in `_write_trace_zip`
  - `python/rustwright/cli.py` → source-checkout fallback in `_version`
  - `python/rustwright/_backend.py` → source-checkout fallback in `_version` (before `+local`)
  - `python/rustwright/_agent/cli.py` → source-checkout fallback
- [ ] Update the docs that quote the version: `java/README.md` quotes the Maven
      coordinates in more than one place.
- [ ] Add a `## [<version>] - <date>` section to `CHANGELOG.md` by renaming the
      current `## [Unreleased]` section and opening a fresh empty one above it,
      add the matching link reference at the bottom, and add the version to
      `REQUIRED_VERSIONS` in `tests/test_changelog.py`.
- [ ] Regenerate the lockfiles; do not edit generated entries by hand. There are
      three Cargo lockfiles, because `cli/` and `mcp/` are separate workspaces
      that depend on `rustwright-core` by path — the root `cargo` command does
      not touch them:

  ```bash
  cargo metadata --format-version 1 > /dev/null
  cargo metadata --manifest-path cli/Cargo.toml --format-version 1 > /dev/null
  cargo metadata --manifest-path mcp/Cargo.toml --format-version 1 > /dev/null
  (cd node && npm install --package-lock-only --ignore-scripts)
  ```

  `cargo metadata` resolves and rewrites a lockfile without compiling, which
  keeps the diff to the version lines instead of churning third-party pins.

- [ ] Confirm every `rustwright*` entry across `Cargo.lock`, `cli/Cargo.lock`,
      and `mcp/Cargo.lock` holds the release version, except `rustwright-cli`
      and `rustwright-mcp`, which version independently. Confirm
      `node/package-lock.json` has it in both top-level version fields.
- [ ] Confirm nothing was missed:

  ```bash
  git grep -n "$PREVIOUS_VERSION" -- . ':!CHANGELOG.md' ':!docs/'
  ```

- [ ] Run local release checks:

  ```bash
  cargo check --locked
  cargo test --locked
  cargo metadata --manifest-path cli/Cargo.toml --locked --format-version 1 > /dev/null
  cargo metadata --manifest-path mcp/Cargo.toml --locked --format-version 1 > /dev/null
  (cd node && npm ci --ignore-scripts && npm run build && npm run smoke)
  ```

Before tagging, every source manifest, the shipped runtime metadata, and all
four lockfiles must have one matching version. The source `node/package.json`
intentionally remains `"private": true`; the npm workflow removes that field
only in its temporary assembled package.

## Dry run

- [ ] Merge the version bump and release setup before tagging.
- [ ] Dry-run **all five** workflows against the release commit, not just PyPI and
      npm. One tag starts all of them, so a workflow you did not dry-run is a
      workflow that first runs for real. For each of **Release Python package**,
      **Release Node.js package**, **Release .NET package**, **Release Ruby
      gem**, and **Release Maven package**, open **Actions → *workflow* → Run
      workflow**, select the release commit, leave `dry_run` checked, and run it.
- [ ] Confirm each run's `validate release metadata` job passed. That job is what
      compares the tree against the tag, so a green metadata job is the signal
      that the version fields are consistent.
- [ ] Download and inspect the build artifacts: `pypi-wheel-*`, `pypi-sdist`,
      `npm-package`, the NuGet `.nupkg`, the platform gems, and the Maven bundle.
      A dispatch with `dry_run: true` never reaches any publish job.

## Publish

- [ ] From an up-to-date, clean checkout of the release commit, use the same version as every manifest:

  ```bash
  VERSION=0.2.0
  git tag -a "v${VERSION}" -m "Rustwright ${VERSION}"
  git push origin "v${VERSION}"
  ```

- [ ] Approve all five GitHub environment deployments: `pypi`, `npm`, `nuget`,
      `rubygems`, and `maven-central`. The one tag starts every workflow;
      publishing is also guarded to `Skyvern-AI/rustwright`.
- [ ] Maven Central publishes are **permanent** — a released coordinate cannot be
      deleted, only superseded. PyPI, npm, NuGet, RubyGems, and crates.io allow
      yanking, which hides a version from resolution without removing it. Treat
      the `maven-central` approval as the point of no return.
- [ ] If a publish job alone must be retried, dispatch that workflow from the existing tag and clear `dry_run`. A branch dispatch cannot publish.
- [ ] Never move or reuse a published version tag. Fix forward with a new version.

## Verify the registries

- [ ] In a clean Python environment, install the exact release and Chromium, then run the quickstart:

  ```bash
  VERSION=0.2.0
  python -m venv /tmp/rustwright-pypi-verify
  /tmp/rustwright-pypi-verify/bin/python -m pip install --upgrade pip
  /tmp/rustwright-pypi-verify/bin/python -m pip install "rustwright==${VERSION}"
  /tmp/rustwright-pypi-verify/bin/python -m rustwright install chromium
  /tmp/rustwright-pypi-verify/bin/python examples/quickstart.py
  ```

- [ ] In a clean Node.js project, install the exact release and load the addon:

  ```bash
  test_dir="$(mktemp -d)"
  (cd "$test_dir" && npm init --yes && npm install "rustwright@${VERSION}" && node -e "require('rustwright')")
  ```

- [ ] Confirm PyPI shows five `cp38-abi3` wheels plus the sdist: macOS arm64/x86_64, manylinux x86_64/aarch64, and Windows x86_64.
- [ ] Confirm npm shows version `${VERSION}` under the `latest` dist-tag and displays provenance. `release-npm.yml` publishes without an explicit `--tag`, so npm applies `latest`.
- [ ] Confirm the remaining three registries list `${VERSION}`: NuGet
      (`Rustwright`), RubyGems (`rustwright`, including one platform gem per
      supported target), and Maven Central
      (`io.github.skyvern-ai:rustwright`). Maven Central can take several hours
      to surface a new coordinate in search after the deployment succeeds.
- [ ] Update the prose that describes a binding as unpublished now that it is
      published — `java/README.md` still frames the Maven coordinates as planned
      and the artifact as unavailable.
- [ ] `rustwright-core` and `rustwright` on crates.io are **not** published by
      any workflow. If this release is meant to reach crates.io, publish both by
      hand from the tagged commit, core first, and confirm the versions match
      the tag. If it is not, record that decision so the gap is deliberate.
- [ ] Record both registry URLs and workflow run URLs on the release tracking issue.
