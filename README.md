# OpenLoadHub

[中文说明](README.zh-CN.md)

OpenLoadHub is a self-hosted control plane for running and observing JMeter and k6 performance tests.

Start with [Quickstart](docs/public/quickstart.md) for the local demo. If you want a page-by-page first run, keep [Demo Walkthrough](docs/public/demo-walkthrough.md) open after the stack starts. If the first run does not reach RunDetail, use [Troubleshooting](docs/public/troubleshooting.md). For shared or traditional cloud deployments, read [Deployment Guide](docs/public/deployment.md).

If you are evaluating whether OpenLoadHub fits your use case, read [FAQ](FAQ.md), [Roadmap](ROADMAP.md), [Known Limitations](KNOWN_LIMITATIONS.md), [Support](SUPPORT.md), and [Open-Core Boundary](docs/public/open-core.md) before treating the alpha as a production platform.

The public alpha focuses on a compact workflow:

- manage scripts and tasks
- run single JMeter and k6 tests
- inspect run detail, plan runs, logs, reports, and dashboard links
- inspect lightweight task and plan configuration lint hints where they are shown; deeper deterministic analysis panels stay hidden in v0.1 alpha
- use Grafana, InfluxDB, and Prometheus for local observability
- use seeded demo tasks and demo plans for out-of-the-box trial runs
- optionally enable webhook notifications for plan runs and threshold events

## Alpha Scope

Included in v0.1 alpha:

- Web UI and API for the core test workflow
- JMeter and k6 execution through a local agent
- RunDetail, PlanRun, report, and log views
- Lightweight task and plan configuration lint hints; deterministic run and batch analysis panels are hidden in v0.1 alpha
- Minimal Docker demo topology with a local HTTP + gRPC target service
- Seeded demo tasks for k6 HTTP+gRPC and JMeter HTTP+gRPC
- Seeded demo plans for JMeter-only and k6+JMeter batch runs
- Public demo feature flags for keeping non-demo modules out of the default workflow

Deferred or optional:

- creating mixed-run execution workflows from scratch
- trend analysis
- self monitoring dashboards
- full alertmanager-centered alerting workflow
- advanced analysis and report-review panels
- Nacos, Kafka, Redis protocol, and xk6 extension demos
- dynamic k6 rate control, planned for v0.2 after the public OpenLoadHub k6 source and reproducible build gate passes

## Roadmap

- v0.1 alpha: self-hosted JMeter / k6 control plane, core task and run workflow, PlanRun, reports, and local observability.
- v0.2 target: dynamic k6 TPS control backed by a public AGPL-compatible OpenLoadHub k6 fork and reproducible build proof.

## 中文说明

OpenLoadHub 是一个自托管压测控制面，面向需要快速搭建 JMeter + k6 压测平台的工程团队。公开 alpha 版本先聚焦脚本、任务、执行、RunDetail、PlanRun、报告和 Grafana / InfluxDB / Prometheus 本地观测闭环。完整中文入口见 [README.zh-CN.md](README.zh-CN.md)。

本地 demo 首次启动后会初始化两条 demo task 和两条 demo plan：

- `OpenLoadHub Demo - k6 HTTP+gRPC`
- `OpenLoadHub Demo - JMeter HTTP+gRPC`
- `OpenLoadHub Demo Plan - JMeter Simple`
- `OpenLoadHub Demo Plan - k6 + JMeter Advanced`

这两条 demo task 默认都会带上关联监控、关联链路和脚本变量元数据；task 级默认 `pod_count=1`、`data_distribution=avg`，而 demo plan 可以在运行时覆盖更高节点数。登录后从 `关注任务` 或 `批次模板` 进入即可直接试跑。

The first public alpha intentionally hides deterministic analysis panels such as run analysis readiness, report quality gates, plan-run delivery advice, batch conclusion panels, and stability conclusions. The current demo keeps only the core result, report, log, Grafana, and lightweight configuration-lint entry points visible; deeper analysis wording will be exposed only after the public UX and docs are tightened.

动态 k6 TPS 调整是后续核心能力，但不会在 v0.1 alpha 中声明为已支持功能；它会在 OpenLoadHub k6 源码、许可证和可复现构建门禁通过后进入 v0.2。

## Repository Shape

This repository is scoped to the open-source OpenLoadHub demo and runtime surface. It excludes:

- private process docs and task packets
- local runtime logs and reports
- private deployment files
- full third-party binary distributions
- custom k6 binaries
- non-public reference materials

See [source and license notes](docs/public/source-and-license.md) for the source disclosure policy.

For runtime topology and optional component decisions, see [Architecture](docs/public/architecture.md) and [Deployment Guide](docs/public/deployment.md).

Webhook setup and template bootstrapping are documented in [Webhook Configuration](docs/public/webhooks.md).

First-run guidance:

- [Quickstart](docs/public/quickstart.md)
- [Demo Walkthrough](docs/public/demo-walkthrough.md)
- [Troubleshooting](docs/public/troubleshooting.md)
- [Examples](docs/public/examples/README.md)
- [Issue Triage](docs/public/issue-triage.md)
- [First Contribution Guide](docs/public/first-contribution.md)

For a useful first-run bug report or validation note, capture the Compose service list, the task or plan page you used, the RunDetail header for the run, and the report or Grafana link that did not behave as expected. The screenshot and redaction checklist lives in [Troubleshooting](docs/public/troubleshooting.md#useful-screenshots-for-bug-reports).

Maintainer release prep:

- [Public Alpha Release Checklist](public-alpha-release-checklist.md)

## Demo Accounts

- OpenLoadHub admin: `admin` / `ptp_demo_admin`
- OpenLoadHub tester: `demo_tester` / `ptp_demo_tester`
- Grafana: `admin` / `ptp_demo_grafana`

Self registration is disabled by default.
