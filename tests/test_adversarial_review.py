from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("adversarial_review", ROOT / "tools" / "adversarial_review.py")
assert SPEC and SPEC.loader
adversarial_review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adversarial_review
SPEC.loader.exec_module(adversarial_review)
INSTALL_SPEC = importlib.util.spec_from_file_location("install_hooks", ROOT / "tools" / "install_hooks.py")
assert INSTALL_SPEC and INSTALL_SPEC.loader
install_hooks = importlib.util.module_from_spec(INSTALL_SPEC)
sys.modules[INSTALL_SPEC.name] = install_hooks
INSTALL_SPEC.loader.exec_module(install_hooks)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test Contributor")
    git(tmp_path, "config", "user.email", "test@users.noreply.github.com")
    (tmp_path / "example.txt").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "example.txt")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "branch", "-M", "main")
    (tmp_path / "example.txt").write_text("base\nchange\n", encoding="utf-8")
    git(tmp_path, "commit", "-qam", "change")
    return tmp_path


def test_resolves_exact_pre_push_revisions(repository: Path):
    base = git(repository, "rev-parse", "HEAD^")
    target = git(repository, "rev-parse", "HEAD")
    resolved = adversarial_review.resolve_review_target(
        repository,
        {
            "PRE_COMMIT_FROM_REF": base,
            "PRE_COMMIT_TO_REF": target,
            "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/main",
            "PRE_COMMIT_REMOTE_NAME": "origin",
        },
    )

    assert resolved == adversarial_review.ReviewTarget(
        base=base,
        target=target,
        remote_branch="refs/heads/main",
        base_object=base,
        target_object=target,
    )


def test_supplied_unknown_revision_fails_closed(repository: Path):
    with pytest.raises(adversarial_review.ReviewError, match="not available"):
        adversarial_review.resolve_review_target(
            repository,
            {
                "PRE_COMMIT_FROM_REF": git(repository, "rev-parse", "HEAD^"),
                "PRE_COMMIT_TO_REF": "1" * 40,
                "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/main",
            },
        )


def test_annotated_tag_object_changes_digest(repository: Path):
    base = git(repository, "rev-parse", "HEAD^")
    git(repository, "tag", "-a", "release", "-m", "first annotation")
    first_object = git(repository, "rev-parse", "release")
    first = adversarial_review.resolve_review_target(
        repository,
        {
            "PRE_COMMIT_FROM_REF": base,
            "PRE_COMMIT_TO_REF": first_object,
            "PRE_COMMIT_REMOTE_BRANCH": "refs/tags/release",
        },
    )
    assert first is not None

    git(repository, "tag", "-f", "-a", "release", "-m", "second annotation")
    second_object = git(repository, "rev-parse", "release")
    second = adversarial_review.resolve_review_target(
        repository,
        {
            "PRE_COMMIT_FROM_REF": base,
            "PRE_COMMIT_TO_REF": second_object,
            "PRE_COMMIT_REMOTE_BRANCH": "refs/tags/release",
        },
    )
    assert second is not None

    assert first.target == second.target
    assert first.target_object != second.target_object
    assert adversarial_review.review_digest(repository, first) != adversarial_review.review_digest(repository, second)


def test_new_disconnected_history_is_reviewed_from_empty_tree(repository: Path):
    git(repository, "checkout", "--orphan", "orphan")
    git(repository, "rm", "-rf", ".")
    (repository / "first.txt").write_text("first\n", encoding="utf-8")
    git(repository, "add", "first.txt")
    git(repository, "commit", "-qm", "first orphan commit")
    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", "second.txt")
    git(repository, "commit", "-qm", "second orphan commit")
    target = git(repository, "rev-parse", "HEAD")

    resolved = adversarial_review.resolve_review_target(
        repository,
        {
            "PRE_COMMIT_FROM_REF": adversarial_review.ZERO_REV,
            "PRE_COMMIT_TO_REF": target,
            "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/orphan",
        },
    )

    assert resolved is not None
    assert resolved.base_is_empty_tree
    changed = git(repository, "diff", "--name-only", resolved.base, resolved.target)
    assert changed.splitlines() == ["first.txt", "second.txt"]


def test_same_commit_new_ref_still_has_a_review_digest(repository: Path):
    target = git(repository, "rev-parse", "HEAD")
    resolved = adversarial_review.resolve_review_target(
        repository,
        {
            "PRE_COMMIT_FROM_REF": adversarial_review.ZERO_REV,
            "PRE_COMMIT_TO_REF": target,
            "PRE_COMMIT_REMOTE_BRANCH": "refs/tags/release-name",
        },
    )

    assert resolved is not None
    assert resolved.base == resolved.target
    assert adversarial_review.review_digest(repository, resolved)


def test_digest_covers_diff_and_commit_metadata(repository: Path):
    base = git(repository, "rev-parse", "HEAD^")
    target = git(repository, "rev-parse", "HEAD")
    review_target = adversarial_review.ReviewTarget(base, target, "refs/heads/main")
    original = adversarial_review.review_digest(repository, review_target)

    git(repository, "commit", "--amend", "-qm", "different metadata")
    amended = adversarial_review.ReviewTarget(base, git(repository, "rev-parse", "HEAD"), "refs/heads/main")

    assert adversarial_review.review_digest(repository, amended) != original


def test_report_rejects_inconsistent_pass():
    report = {
        "verdict": "pass",
        "summary": "inconsistent",
        "findings": [
            {
                "severity": "blocking",
                "file": "example.txt",
                "line": 1,
                "title": "must fix",
                "detail": "details",
            }
        ],
    }

    with pytest.raises(adversarial_review.ReviewError, match="blocking findings"):
        adversarial_review.parse_report(json.dumps(report))


def test_receipt_is_tied_to_exact_review_target(repository: Path, tmp_path: Path):
    base = git(repository, "rev-parse", "HEAD^")
    target = git(repository, "rev-parse", "HEAD")
    review_target = adversarial_review.ReviewTarget(base, target, "refs/heads/main")
    digest = adversarial_review.review_digest(repository, review_target)
    path = tmp_path / "receipt.json"

    adversarial_review.write_receipt(path, review_target, digest, "codex")

    assert adversarial_review.valid_receipt(path, review_target, digest)
    changed_branch = adversarial_review.ReviewTarget(base, target, "refs/heads/release")
    assert not adversarial_review.valid_receipt(path, changed_branch, digest)


def test_console_findings_do_not_echo_detail():
    sensitive_detail = "suspected credential value must never be printed"
    lines = adversarial_review.safe_finding_lines(
        {
            "findings": [
                {
                    "severity": "blocking",
                    "file": "config.txt",
                    "line": 4,
                    "title": "credential committed",
                    "detail": sensitive_detail,
                }
            ]
        }
    )

    assert lines == ["- blocking: config.txt:4: credential committed"]
    assert sensitive_detail not in "\n".join(lines)


def test_pre_push_dispatcher_reviews_every_ref(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "reviews.log"
    fake_pre_commit = fake_bin / "pre-commit"
    fake_pre_commit.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s %s %s skip=%s\\n' \"$PRE_COMMIT_FROM_REF\" \"$PRE_COMMIT_TO_REF\" "
        "\"$PRE_COMMIT_REMOTE_BRANCH\" \"${SKIP-}\" >> \"$REVIEW_LOG\"\n",
        encoding="utf-8",
    )
    fake_pre_commit.chmod(0o755)
    updates = "\n".join(
        (
            f"refs/heads/main {'1' * 40} refs/heads/main {'2' * 40}",
            f"refs/tags/v1 {'3' * 40} refs/tags/v1 {'0' * 40}",
        )
    )

    result = subprocess.run(
        ["bash", str(ROOT / ".githooks" / "pre-push"), "origin", "example.invalid"],
        cwd=ROOT,
        input=updates + "\n",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REVIEW_LOG": str(log),
            "SKIP": "adversarial-review",
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"{'2' * 40} {'1' * 40} refs/heads/main skip=",
        f"{'0' * 40} {'3' * 40} refs/tags/v1 skip=",
    ]


def test_hook_installer_sets_worktree_specific_path(repository: Path):
    hooks_dir = repository / ".githooks"
    hooks_dir.mkdir()
    dispatcher = hooks_dir / "pre-push"
    dispatcher.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    dispatcher.chmod(0o755)

    installed = install_hooks.install(repository, hooks_dir)

    assert installed != dispatcher
    assert installed.read_bytes() == dispatcher.read_bytes()
    assert installed.parent.name == "codex-hooks"
    assert git(repository, "config", "--get", "extensions.worktreeConfig") == "true"
    assert git(repository, "config", "--worktree", "--get", "core.hooksPath") == str(installed.parent)
