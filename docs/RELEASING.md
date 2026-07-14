# Releasing Rustwright

This guide is for the release owner.

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

No crates.io settings or token are currently required. Do not publish `rustwright-core` yet: it is tightly coupled to PyO3, has only a small Rust-facing API, and would commit the team to Rust API compatibility, documentation, security advisories, and an additional release channel. Reconsider after the core is cleanly separated and there are committed Rust users; then add a dedicated crates.io workflow and a narrowly scoped `CARGO_REGISTRY_TOKEN`.

## Prepare a release

- [ ] Choose a SemVer version. Prereleases (for example `0.2.0-alpha.1`) publish to the npm `next` dist-tag; stable versions (for example `0.1.0`) publish to `latest`.
- [ ] Set that exact string in these source-of-truth fields:
  - `pyproject.toml` → `[project].version`
  - `Cargo.toml` → `[package].version` for `rustwright-core`
  - `node/Cargo.toml` → `[package].version` for `rustwright-node`
  - `node/package.json` → `version`
- [ ] Regenerate the lockfiles; do not edit generated entries by hand:

  ```bash
  cargo check
  (cd node && npm install --package-lock-only --ignore-scripts)
  ```

- [ ] Confirm `Cargo.lock` now has the same version for `rustwright-core` and `rustwright-node`, and `node/package-lock.json` has it in both top-level version fields.
- [ ] Confirm all six files are staged: `pyproject.toml`, `Cargo.toml`, `node/Cargo.toml`, `node/package.json`, `Cargo.lock`, and `node/package-lock.json`.
- [ ] Run local release checks:

  ```bash
  cargo check --locked
  cargo test --locked
  (cd node && npm ci --ignore-scripts && npm run build && npm run smoke)
  ```

The four source manifests and both lockfiles are all `0.1.0` today; there is no current version mismatch. The source `node/package.json` intentionally remains `"private": true`; the npm workflow removes that field only in its temporary assembled package.

## Dry run

- [ ] Merge the version bump and release setup before tagging.
- [ ] In **Actions → Release Python package → Run workflow**, select the release commit, leave `dry_run` checked, and run it.
- [ ] In **Actions → Release Node.js package → Run workflow**, select the same commit, leave `dry_run` checked, and run it.
- [ ] Download and inspect `pypi-wheel-*`, `pypi-sdist`, and `npm-package`. A dispatch with `dry_run: true` never reaches either publish job.

## Publish

- [ ] From an up-to-date, clean checkout of the release commit, use the same version as every manifest:

  ```bash
  VERSION=0.2.0-alpha.1
  git tag -a "v${VERSION}" -m "Rustwright ${VERSION}"
  git push origin "v${VERSION}"
  ```

- [ ] Approve the `pypi` and `npm` GitHub environment deployments. The tag starts both workflows; publishing is also guarded to `Skyvern-AI/rustwright`.
- [ ] If a publish job alone must be retried, dispatch that workflow from the existing tag and clear `dry_run`. A branch dispatch cannot publish.
- [ ] Never move or reuse a published version tag. Fix forward with a new version.

## Verify the registries

- [ ] In a clean Python environment, install the exact release and Chromium, then run the quickstart:

  ```bash
  VERSION=0.2.0-alpha.1
  python -m venv /tmp/rustwright-pypi-verify
  /tmp/rustwright-pypi-verify/bin/python -m pip install --upgrade pip
  /tmp/rustwright-pypi-verify/bin/python -m pip install --pre "rustwright==${VERSION}"
  /tmp/rustwright-pypi-verify/bin/python -m rustwright install chromium
  /tmp/rustwright-pypi-verify/bin/python examples/quickstart.py
  ```

- [ ] In a clean Node.js project, install from the experimental dist-tag and load the addon:

  ```bash
  test_dir="$(mktemp -d)"
  (cd "$test_dir" && npm init --yes && npm install rustwright@next && node -e "require('rustwright')")
  ```

- [ ] Confirm PyPI shows five `cp38-abi3` wheels plus the sdist: macOS arm64/x86_64, manylinux x86_64/aarch64, and Windows x86_64.
- [ ] Confirm npm shows version `${VERSION}` under the `next` dist-tag and displays provenance.
- [ ] Record both registry URLs and workflow run URLs on the release tracking issue.
