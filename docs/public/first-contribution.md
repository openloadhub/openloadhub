# First Contribution Guide

The best early contributions are small, focused, and easy to review.

## Good First Issues

Good first issues usually include:

- documentation fixes
- clearer quickstart wording
- troubleshooting notes for common Docker or browser problems
- example improvements
- UI copy clarification
- small test coverage additions around public alpha behavior

Use the good-first-issue template when proposing a starter task. Maintainers will add `good first issue` only after the work is narrow, public, and verifiable without private context; see [Issue Triage](issue-triage.md).

For a first public docs contribution, prefer one narrow change such as a missing port note, a clearer first-run step, a screenshot redaction note, or a correction to an example command. Avoid mixing docs polish, runtime behavior, and roadmap discussion in one pull request.

## Pull Request Expectations

Before opening a pull request:

- keep the change focused
- explain the user-facing problem
- include the commands you ran
- attach sanitized screenshots or logs when the UI changes
- sign off commits with DCO

For docs-only changes, the minimum useful verification is:

```bash
git diff --check
```

If you touched public alpha docs, also scan the edited text for accidental private material or roadmap-only features described as available. Keep public docs free of private process notes, local absolute paths, credentials, customer data, and unpublished implementation plans.

Example:

```bash
git commit -s -m "docs: clarify local demo ports"
```

## Changes That Need Maintainer Discussion First

Open an issue before starting work on:

- new deployment modes
- source-gated dynamic k6 TPS roadmap work
- mixed-run execution
- enterprise identity or audit features
- new runtime dependencies
- large UI rewrites
- new plugin systems

If you are unsure whether a change is scope expansion or documentation cleanup, open a short issue first with the problem, the proposed doc page, and the exact command or screenshot that confused you.

## Keep Public Materials Clean

Do not include secrets, private hostnames, private screenshots, customer data, internal process notes, or local absolute paths.

When adding screenshots or logs to an issue or pull request, prefer the local demo URLs from [Quickstart](quickstart.md), redact everything else, and include the OpenLoadHub commit plus Docker and browser versions.
