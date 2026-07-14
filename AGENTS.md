# Repository Instructions

## Public boundary

Treat every tracked file, commit, branch name, and review artifact as if it will
be publicly visible.

- Never add credentials, auth headers, customer data, private URLs, personal
  filesystem paths, employee email addresses, internal ticket identifiers, or
  private host/network topology.
- Do not name private repositories, packages, modules, services, or operational
  incidents. Use neutral placeholders such as `<organization>`, `<repository>`,
  `<remote-host>`, and `<source-checkout>` in public examples.
- Keep integration tools that import or inspect private code in the private
  repository. Public tools must require explicit user-supplied paths and must
  not default to sibling checkouts.
- Keep raw benchmark data, terminal output, HAR files, environment files, and
  generated reports untracked. Publish only reviewed, reproducible summaries.
- Use a GitHub `noreply` address for commit author and committer identities.
  Automation co-author trailers must likewise use a provider `noreply` address
  (for example `noreply@anthropic.com`), never a personal or corporate mailbox.
  Never rewrite or force-push
  shared history without explicit owner approval, and never use `git push --all`.

Before committing, inspect the staged diff for disclosure risks. Before pushing,
allow the configured adversarial review hook to inspect the exact outgoing
revision. Do not bypass it with `--no-verify` or `SKIP`.

## Adversarial review

Install the push hook with `python tools/install_hooks.py`. The tracked
`.githooks/pre-push` dispatcher is copied into the worktree's private Git
directory so checking out a revision that removes the tracked file cannot
silently disable it. It runs the hook in `.pre-commit-config.yaml` once for every
pushed ref, which in turn runs `tools/adversarial_review.py`. The gate:

1. identifies the exact remote and local objects supplied by Git for each ref;
2. reuses a passing receipt only when the revision, commit metadata, and diff
   digest are unchanged;
3. invokes a read-only Codex reviewer and blocks the push on findings, malformed
   output, timeout, quota exhaustion across both runtimes, or any other failure.

Review output must identify locations and categories without echoing suspected
secret values. A Git hook is a local guardrail, not a security boundary; CI and
repository rules should run independent checks for public releases.

## Codex subagents

Run Codex subagents through one of the configured runtimes from `~/.zshrc`:
`codex` or `ikonomos-codex`. If the selected runtime is unavailable or out of
usage, retry the other configured runtime.

`tools/adversarial_review.py` defaults to the `codex` runtime only, because the
tool is mirrored to the public repository and must not name local runtimes. To
give the pre-push gate a fallback runtime locally, export a comma-separated
list in `~/.zshrc`, e.g. `export RUSTWRIGHT_REVIEW_RUNTIMES="codex,ikonomos-codex"`.
