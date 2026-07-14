#!/usr/bin/env python3
"""Install this repository's multi-ref pre-push dispatcher."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed with exit code {result.returncode}")
    return result.stdout.strip()


def effective_hooks_dir(root: Path) -> Path:
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    configured = result.stdout.strip()
    if result.returncode == 0 and configured:
        path = Path(os.path.expanduser(configured))
        return path if path.is_absolute() else (root / path).resolve()
    return Path(run(root, "rev-parse", "--git-path", "hooks")).resolve()


def install(repo: Path = ROOT, hooks_dir: Path | None = None) -> Path:
    root = Path(run(repo, "rev-parse", "--show-toplevel"))
    source_dir = (hooks_dir or root / ".githooks").resolve()
    source = source_dir / "pre-push"
    if not source.is_file():
        raise RuntimeError(f"pre-push dispatcher not found: {source}")
    if not os.access(source, os.X_OK):
        raise RuntimeError(f"pre-push dispatcher is not executable: {source}")

    git_dir = Path(run(root, "rev-parse", "--absolute-git-dir"))
    installed_dir = git_dir / "codex-hooks"
    previous_dir = effective_hooks_dir(root)
    installed_dir.mkdir(parents=True, exist_ok=True)

    # core.hooksPath redirects EVERY hook lookup, so any hook living in the
    # previously effective directory must be mirrored here or Git would
    # silently stop running it.
    if previous_dir != installed_dir and previous_dir.is_dir():
        for hook in sorted(previous_dir.iterdir()):
            if not hook.is_file() or hook.name.endswith(".sample"):
                continue
            if hook.name == "pre-push":
                if hook.read_bytes() == source.read_bytes():
                    continue
                chained = installed_dir / "pre-push.chained"
                shutil.copy2(hook, chained)
                chained.chmod(0o755)
                print(
                    f"Chained existing pre-push hook: {hook} "
                    "(runs after the adversarial review)"
                )
                continue
            mirrored = installed_dir / hook.name
            shutil.copy2(hook, mirrored)
            mirrored.chmod(0o755)
            print(f"Mirrored existing Git hook: {hook.name}")

    dispatcher = installed_dir / "pre-push"
    shutil.copy2(source, dispatcher)
    dispatcher.chmod(0o755)

    run(root, "config", "extensions.worktreeConfig", "true")
    run(root, "config", "--worktree", "core.hooksPath", str(installed_dir))
    configured = Path(run(root, "config", "--worktree", "--get", "core.hooksPath"))
    if configured != installed_dir:
        raise RuntimeError("Git did not retain the worktree-specific hooks path")
    return dispatcher


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        dispatcher = install()
    except RuntimeError as exc:
        print(f"Hook installation failed: {exc}")
        return 1
    print(f"Installed worktree-specific pre-push dispatcher: {dispatcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
