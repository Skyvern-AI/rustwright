---
name: version-upgrade
description: Prepare, validate, and publish a Rustwright version across the Python/PyPI and Node.js/npm packages. Use when asked to bump or upgrade the Rustwright version, prepare a release PR, tag a release, publish Rustwright, or verify both package registries.
---

# Version Upgrade

Release one Rustwright version through the repository's existing PyPI and npm
workflows. Treat "npm and Node.js" as one target: npm is the registry for the
Node.js package. The two release targets are Python on PyPI and Node.js on npm.
Do not publish `rustwright-core` to crates.io.

## Arguments and mode

Read `$ARGUMENTS` for an exact SemVer version and an optional mode.

- Accept stable versions such as `0.1.1` and prereleases in the shared
  Cargo/npm/PyPI subset: `0.2.0-alpha.1`, `0.2.0-beta.1`, or
  `0.2.0-rc.1`. Reject other SemVer prerelease labels because PyPI may not
  accept or may ambiguously normalize them.
- If the caller asks only to check, verify, or report a release or registry
  status, enter verify-only mode. Perform read-only GitHub, Git, PyPI, and npm
  queries and return the result without creating a branch, changing a file,
  dispatching a workflow, merging, tagging, or publishing.
- If no version is supplied, use the helper's default: increment a stable
  patch version or the final numeric prerelease component.
- Treat `prepare`, `PR only`, or `dry run` as prepare-only mode. Stop after the
  release PR and both successful preview artifact dry runs.
- Enter full-release mode only when the caller explicitly says `publish`,
  `release now`, or `full release` in the current request. Invoking this skill,
  asking for a version bump, or omitting a mode never by itself authorizes an
  irreversible registry publication; default those cases to prepare-only mode.
- Do not pause for choices that can be derived from the repository. Stop only
  for a dirty worktree, missing authorization or secret, failed validation, a
  required human review, or another condition that makes publishing unsafe.

## 1. Inspect current state

1. Read `AGENTS.md` and `docs/RELEASING.md`; repository instructions and the
   current workflows override examples in this skill.
2. Require a clean worktree. Never discard local changes.
3. Fetch `origin/main` and all tags. Start from the current `origin/main`, not a
   stale local branch.
4. Inspect `.github/workflows/release-pypi.yml` and
   `.github/workflows/release-npm.yml`. Both must still publish from the same
   `v<version>` tag.
5. Search open and merged release PRs, Git tags, PyPI, and npm before deciding
   where to start. For PyPI, compare every release key after canonicalizing
   both sides with `packaging.version.Version`; for example, PyPI may represent
   `0.2.0-alpha.1` as the equivalent `0.2.0a1`. Compare npm versions with
   SemVer rules.
6. Require monotonic releases across all three histories. A new target must be
   strictly newer than every release version in Git tags, PyPI, and npm. A
   prepared or tagged target may equal the newest version, but must never be
   published if any newer release already exists in any history. Treat an
   invalid `v*` release tag as a blocker rather than silently ignoring it.
7. Classify the target as new, on an open release PR, prepared on main, tagged
   or publishing, partially published, or fully published. Reuse an existing
   PR only after inspecting its complete commit history and diff. Its head must
   be in this repository, its base must be current `main`, every version field
   must equal the target, and its diff must contain only the expected version
   files. A filename allowlist is not sufficient: inspect every hunk and
   require changes only to the exact version fields plus the two local package
   entries regenerated in the lockfiles. Reject dependency changes, unrelated
   lines in an allowed file, mode changes, renames, binaries, or extra commit
   content. When uncertain, reproduce the bump from the PR base in a clean
   temporary worktree and compare the resulting patch. Rerun every check and
   preview against its current head. Treat an unexpected file, commit, base,
   or fork as a blocker rather than inheriting it. If the target is already
   consistent on `origin/main` but has no tag or
   registry publication, treat it as a prepared release and resume at the
   final merged-commit dry runs in section 4. If it is fully published, report
   success without mutation. Handle partial publication as described below.
   Never reuse an equivalent published version or move a release tag.
8. Before resuming any existing tag, validate its provenance. Require an
   annotated `v<version>` tag whose target commit is reachable from current
   `origin/main`, contains the same target in every version field, and matches
   the `headSha` of its tag-triggered release runs. Stop on a lightweight tag,
   mismatched metadata or run SHA, unreachable commit, or unexpected tag
   target. Do not approve or rerun publication from an unverified tag.

## 2. Prepare the version bump

For a new target only, create `release/v<version>` from `origin/main`. Use the
repository helper to update the four source manifests and shipped runtime
metadata:

```bash
python3 .claude/skills/version-upgrade/scripts/bump_version.py <version>
```

Omit `<version>` only when the caller did not specify one. Then regenerate,
rather than manually edit, both lockfiles:

```bash
cargo check
(cd node && npm install --package-lock-only --ignore-scripts)
python3 .claude/skills/version-upgrade/scripts/bump_version.py --check <version>
```

The check must confirm one exact version in all of these locations:

- `pyproject.toml`
- `Cargo.toml`
- `node/Cargo.toml`
- `node/package.json`
- the Rustwright creator version in `python/rustwright/sync_api.py`
- the Rustwright trace version in `python/rustwright/sync_api.py`
- the source-checkout fallback in `python/rustwright/cli.py`
- the source-checkout fallback in `python/rustwright/_backend.py`
- the `rustwright-core` and `rustwright-node` entries in `Cargo.lock`
- both top-level package versions in `node/package-lock.json`

Run the release checks from `docs/RELEASING.md`:

```bash
cargo check --locked
cargo test --locked
(cd node && npm ci --ignore-scripts && npm run build && npm run smoke)
```

Do not weaken, skip, or silently narrow a failing check.

## 3. Push and dry-run both packages

1. Review the complete diff and staged diff for accidental disclosures.
2. Commit only the nine expected version files listed above. Use the
   authenticated GitHub account's
   public `users.noreply.github.com` identity as required by `AGENTS.md`.
3. Install the tracked pre-push hook with `python3 tools/install_hooks.py`.
4. Push the release branch without bypassing hooks and open a PR titled
   `chore(release): bump version to <version>`.
5. Dispatch both release workflows against the release branch with
   `dry_run=true`. Capture both run URLs and wait for both runs to succeed.
   These preview runs must build the PyPI wheels/sdist and the assembled npm
   tarball; they must not enter either publish job. They are early artifact
   checks only and do not validate the merged commit that will be tagged.
6. Add the successful preview run URLs to the PR description or a PR comment.

If either dry run fails, diagnose it, fix the release branch, rerun both when a
shared input changed, and do not merge until both pass.

## 4. Merge and publish

Skip this section in prepare-only mode.

1. Wait for required PR checks. Merge using the repository's normal merge
   policy; do not bypass required reviews or protections.
2. Refresh `origin/main`, choose its current commit as the release candidate,
   verify that commit contains the exact target version in all manifests and
   lockfiles, and verify the worktree is clean.
3. Dispatch both release workflows against `main` with `dry_run=true`. Capture
   each run's `headSha`; both must equal the same release-candidate commit and
   both runs must succeed. If the runs resolve to different commits or main
   moves before dispatch, refresh main and rerun both against one new candidate.
   A successful release-branch preview is never a substitute for these final
   merged-commit dry runs.
4. Re-fetch main immediately before tagging and require `origin/main` to still
   equal the tested candidate. If main moved for any reason, select its new
   head and repeat both final dry runs; do not knowingly publish a stale
   candidate. Re-verify the target version at the unchanged candidate, then
   create an annotated `v<version>` tag on it using the GitHub noreply identity.
   Push only that tag. Never retag a branch commit, unmerged commit, stale
   commit, or untested commit.
5. Locate the tag-triggered runs of `release-pypi.yml` and `release-npm.yml` and
   monitor both to completion.
6. If an `npm` or `pypi` environment approval is pending, approve it only when
   the authenticated actor is an allowed reviewer and the invocation is in
   full-release mode. Never change environment protections or repository
   secrets to make approval succeed. If human approval is mandatory, report
   the two run URLs and the exact pending environments.

If a final merged-commit dry run fails, fix forward with a new PR and repeat the
final dry runs. Do not create the release tag first.

If one registry publishes and the other fails, keep the tag unchanged. For a
transient failure or corrected environment/secret, rerun the failed jobs or
dispatch only the failed workflow from the existing tag with `dry_run=false`,
as documented in `docs/RELEASING.md`. A workflow dispatch from the old tag does
not consume workflow-code fixes merged later. If workflow code or package
source must change, merge the fix, choose a new version, and publish both
targets from a new immutable tag; record the old version as partially
published. Never move the old tag to pick up a fix.

## 5. Verify and report

Verify the exact version through the PyPI JSON API and `npm view`. For npm,
stable versions must be on `latest` and prereleases must be on `next`. Run the
clean-install smoke tests from `docs/RELEASING.md` when practical.

Return a compact release summary containing:

- version and tag;
- release PR;
- PyPI and npm workflow runs;
- PyPI and npm package URLs;
- verification status or the single concrete blocker.
