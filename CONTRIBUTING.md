# Contributing

OpenLoadHub welcomes focused bug reports, documentation fixes, and small pull requests that improve the public alpha workflow.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md), [Support](SUPPORT.md), [Issue Triage](issue-triage.md), and [First Contribution Guide](first-contribution.md) before opening your first issue or pull request.

## Development Setup

```bash
cp .env.example .env
docker compose -f docker-compose.demo.yml up -d --build
```

Run the public shape checks before opening a pull request when your change affects exported files:

```bash
docker compose -f docker-compose.demo.yml config
```

## Pull Request Scope

Please keep pull requests small and describe:

- the user-facing change
- the commands you ran
- any screenshots or logs that prove the change

The v0.1 alpha does not accept changes that depend on Nacos, Kafka, Redis, custom k6 binaries, or dynamic k6 TPS control as default demo features.

## Issues And Labels

Use the bug, feature request, or good-first-issue templates. Maintainers triage public issues with the label set in [Issue Triage](issue-triage.md); new issues should stay focused on public alpha behavior that can be reproduced from the local demo or exported source.

## DCO Sign-Off

OpenLoadHub uses Developer Certificate of Origin sign-off for contributions during the public alpha. By signing off, you certify that you have the right to submit the contribution under the project license.

Use:

```bash
git commit -s -m "docs: clarify local demo setup"
```

Pull requests without `Signed-off-by:` lines may be asked to amend commits before review.

## Maintainer Review

Maintainers may close or defer changes that expand v0.1 alpha scope, add unclear runtime dependencies, weaken public source or license boundaries, or require private deployment context to validate.
