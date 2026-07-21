# Repository Instructions

## Public boundary

Treat every tracked file, commit, branch name, and review artifact as if it will
be publicly visible. This repository is the source of truth; a separate,
reviewed sync republishes approved content to a public mirror, so every change
merged here is the last gate before it can become public.

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
  generated reports untracked. Publish only reviewed, reproducible summaries;
  never let a headline number rest on demo-grade or otherwise irreproducible
  data.
- Use a GitHub `noreply` address for commit author and committer identities.
  Automation co-author trailers must likewise use a provider `noreply` address
  (for example `noreply@anthropic.com`), never a personal or corporate mailbox.
  Never rewrite or force-push shared history without explicit owner approval,
  and never use `git push --all`.

Before committing, inspect the staged diff for disclosure risks.

## Pull-request review

Development checkouts push only to this repository — there is no direct push to
the public mirror; the reviewed sync is the only path content takes to become
public. Every pull request opened here is reviewed automatically by
`.github/workflows/claude-code-review.yml`, which loads its prompt from
`.github/prompts/code-review.md` on the pull request's **base** branch (so an
untrusted pull request cannot alter its own review) and posts a single comment.

That review includes a pre-public leak & integrity gate reported under a
`🔴 Critical Issues` section. It flags the disclosure risks listed above,
removals that leave broken imports, tests, scripts, docs, workflows, packaging,
or release gates behind, and published claims or headline numbers that rest on
irreproducible data. Review output identifies categories and locations without
echoing suspected secret values.

The gate is **advisory, not blocking**: a human maintainer may merge over a
Critical finding when they judge it a false positive or an acceptable, justified
risk. Treat a Critical finding as a strong signal to fix or explicitly justify
before merging. Automated review is a guardrail, not a security boundary;
repository rules and the publish-time sync remain the final checks before
anything reaches the public mirror.

## Local verification

Run lightweight checks locally: `cargo check --locked`, `cargo test --lib`,
`maturin develop --release`, and targeted pytest subsets.

Do not run heavyweight verification on a developer host. This includes Docker
verification-image builds, `tools/docker_test.sh` or `tools/docker_verify.sh`
container modes, full pytest runs, and benchmark workloads. Run these checks in
Blacksmith test containers: preflight with
`python tools/check_testbox_visibility.py --json`, warm a testbox with
`tools/run_benchmark_testbox.sh` through
`.github/workflows/benchmark-testbox.yml`, and execute with
`blacksmith testbox run --id <ID> "<command>"`. Alternatively, use a
dispatch-only workflow on a Blacksmith runner.

Follow `BENCHMARK.md` for benchmarks. Use a testbox first; never run benchmarks
on the developer host.
