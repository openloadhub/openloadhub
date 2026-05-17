# Public Alpha Release Checklist

Use this checklist before publishing an OpenLoadHub public alpha repository or release tag.

## Repository Setup

- Public namespace and repository name are confirmed.
- Initial import uses a clean snapshot, not the private repository history.
- Default branch is protected.
- Required status checks are configured.
- Issues and private vulnerability reporting are enabled.
- Issue templates are present.
- Public issue labels are created from `.github/labels.yml`.
- Good-first-issue intake is enabled through the issue template.

## Governance

- `LICENSE` is present and matches the intended platform license.
- `SECURITY.md` is present.
- `CONTRIBUTING.md` includes DCO sign-off rules.
- `CODE_OF_CONDUCT.md` is present.
- `SUPPORT.md` explains best-effort community support.
- `FAQ.md`, `ROADMAP.md`, and `KNOWN_LIMITATIONS.md` are present.
- The open-core boundary is documented.

## Public Narrative

- README explains who the project is for.
- Quickstart gives a short first-run path.
- Demo walkthrough explains the first k6 and JMeter runs.
- Roadmap-only features are not described as available.
- Known limitations are explicit.
- Public docs avoid private process, private deployment, source-rewrite, and internal workflow narratives.

## Release Gates

- Clean export has been regenerated.
- Public risk scan passes.
- Repository shape check passes.
- Demo Compose config validates.
- Demo smoke passes.
- Public import prep has created a fresh local import commit.
- Release tag and release notes reference the same commit.

## After Publishing

- Verify clone-from-public quickstart.
- Verify GitHub security advisory intake.
- Pin the first public release tag.
- Create first good-first-issue candidates.
- Keep public issue triage separate from private implementation planning.
