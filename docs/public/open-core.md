# Open-Core Boundary

OpenLoadHub is intended to use an open-core model.

## Community Edition

The public community edition is expected to include:

- core self-hosted control plane for JMeter and k6
- Web UI and API for scripts, tasks, runs, RunDetail, reports, logs, and dashboard links
- local demo Compose stack
- demo target service and seeded demo tasks
- public documentation, examples, and smoke checks
- webhook bootstrap examples
- plugin and extension interface documentation as it matures
- reproducible benchmark or demo scripts when they are source-safe and documented

## Reserved For Future Commercial Or Enterprise Offerings

The following may be kept outside the community edition:

- managed cloud service
- enterprise deployment packages and operational runbooks
- commercial plugins
- custom implementation packages
- private data assets
- security-sensitive operational logic
- enterprise SSO, audit, approval, and compliance governance
- guaranteed support or production incident response

## Suitable For

- teams that want a self-hosted JMeter and k6 control plane
- engineers evaluating a unified task, run, report, and observability workflow
- small teams that can operate their own Docker-based stack
- contributors who want to improve examples, docs, UI clarity, and test coverage

## Not Suitable For

- teams expecting a managed SaaS from the community repo alone
- production HA deployments without their own validation
- dynamic fleet scheduling or autoscaling use cases in v0.1
- enterprise identity, audit, and compliance requirements out of the box

## Source Availability Promise

Released Apache-2.0 community source remains available under Apache-2.0. Future commercial services or enterprise features may be distributed separately, but already published community releases are not retroactively closed.
