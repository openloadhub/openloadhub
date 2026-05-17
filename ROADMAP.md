# Roadmap

This roadmap is a direction statement, not a delivery guarantee. Items may move as the public alpha receives real feedback.

## v0.1 Alpha

- Self-hosted local demo with Docker Compose
- Web UI and API for scripts, tasks, runs, RunDetail, reports, logs, and dashboard links
- JMeter and k6 execution through local agents
- Seeded k6 HTTP+gRPC and JMeter HTTP+gRPC demo tasks
- Seeded JMeter-only and k6+JMeter demo plans
- Grafana, Prometheus, InfluxDB, MySQL, Redis, and a local HTTP+gRPC demo target
- Webhook notification bootstrap, disabled by default
- Roadmap-only placeholders for mixed runs, trend analysis, and Self-APM

## v0.1.x

- Better first-run documentation, screenshots, and troubleshooting notes
- More focused examples for task scripts, webhook templates, and deployment overrides
- Public issue triage labels and good-first-issue guidance
- Demo smoke and repository shape checks tightened around release readiness
- UI copy and onboarding polish based on public feedback

## v0.2 Target

- Dynamic k6 TPS control after the OpenLoadHub k6 source, license notice, tag, build command, and reproducible build proof are public
- Clearer plugin and extension interface documentation
- More complete mixed-run and trend-analysis UX, if the public alpha workflow proves understandable
- Optional deployment profiles only after they have reproducible proof and documentation

## Later

- Enterprise identity integration patterns such as OIDC or LDAP adapters
- High-availability deployment packages
- Stronger audit, approval, and role governance
- Cloud service or managed offering
- Commercial plugins and deployment support

## Not Promised In v0.1

- Turnkey production high availability
- Dynamic agent discovery
- Full SSO bundle
- Built-in enterprise audit program
- Default Nacos, Kafka, SkyWalking, Alertmanager, or MinIO stack
- Product AI report features
