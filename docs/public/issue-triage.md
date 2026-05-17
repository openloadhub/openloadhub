# Issue Triage

This page defines the public alpha issue flow for OpenLoadHub maintainers and contributors.

## Issue Types

Use the issue templates instead of blank issues:

- bug reports for reproducible failures in the public alpha demo or exported source
- feature requests for focused improvements to an existing public alpha workflow
- good-first-issue proposals for small tasks that do not require private context

Security vulnerabilities must not be filed as public issues. Use the security reporting path in `SECURITY.md`.

## Labels

The public label set is defined in `docs/public/labels.yml` and exported to `.github/labels.yml`.

Preview the label commands before applying them:

```bash
python3 scripts/sync-openloadhub-labels.py --repo openloadhub/openloadhub
```

After the public repository is synchronized and the owner has approved repository governance writes, apply the labels explicitly:

```bash
python3 scripts/sync-openloadhub-labels.py --repo openloadhub/openloadhub --apply
```

Minimum triage labels:

- `needs triage`: default intake state
- `type bug`, `type enhancement`, or `type docs`
- one area label such as `area demo`, `area frontend`, `area backend`, or `area docs`

Optional labels:

- `good first issue`: small and reviewable by a new contributor
- `help wanted`: maintainer-approved community task
- `blocked`: waiting for reproduction, logs, screenshots, or maintainer decision
- `support`: usage question inside the public alpha support boundary

## Good First Issue Criteria

A good first issue should:

- require no private deployment, private workflow, or production access
- fit in one docs page, one small UI copy change, one example, or one focused test
- include the expected file path or page
- include one verification command or manual check
- avoid v0.2 roadmap work such as dynamic k6 TPS, plugin interfaces, mixed-run UX, or enterprise identity

Good candidates include:

- clarifying a quickstart step
- improving troubleshooting for Docker, browser, or Grafana login problems
- adding a sanitized screenshot checklist
- tightening public alpha copy that confused a first-run user
- improving one webhook or demo configuration example

## Maintainer Flow

1. Confirm the issue uses a template and contains a public reproduction.
2. Add `needs triage`, a type label, and one area label.
3. Ask for missing logs, screenshots, Docker version, browser version, or commit before assigning.
4. Add `good first issue` only after the task is narrow enough for a new contributor.
5. Close or mark `wontfix` when the request depends on private infrastructure, secrets, production incident response, or deferred v0.2 features.

## Public Alpha Boundary

Do not use public issues to promise private-only capabilities or roadmap items as delivered features. Keep issue discussion tied to the public alpha repository, local Docker demo, public docs, exported source, and reproducible public evidence.
