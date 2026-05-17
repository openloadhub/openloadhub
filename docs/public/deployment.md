# Deployment Guide

> This guide covers shared and traditional cloud deployments. For a local demo, use [Quickstart](quickstart.md).
>
> 中文说明：本文用于共享环境或传统云平台部署；本地体验请先看 [Quickstart](quickstart.md)。

## Deployment Modes

| Mode | Use case | Storage | Agent dispatch |
| --- | --- | --- | --- |
| Local demo | Laptop or single developer machine | Docker volumes | Four static compose agents |
| Single VM | Small team, one Linux VM | Host volumes or S3-compatible object storage | Static agent list |
| Traditional cloud | VM group, managed DB / Redis, object storage, reverse proxy | S3-compatible object storage or MinIO | Static runtime agents such as `agent-a:9096,agent-b:9096` |

The public v0.1 alpha proves the local fixed four-agent demo. Larger multi-agent and high-availability deployments should be validated in your own environment before production use.

中文：v0.1 alpha 已验证的是本地固定四 agent demo。更多 agent 或高可用部署可以按本文配置方向推进，但正式上线前需要在自己的环境补充验证。

## Required Services

| Service | Required | Notes |
| --- | --- | --- |
| Web UI | Yes | Serve behind HTTPS in shared environments. |
| Admin API | Yes | Control-plane API, auth, and orchestration. |
| Worker | Yes | Celery worker for background dispatch and polling. |
| Execution Agent | Yes | Runs JMeter and k6 tests. The local demo starts four fixed agents. |
| MySQL | Yes | Use managed MySQL or an HA MySQL deployment for shared environments. |
| Redis | Yes | Celery broker and result backend. Managed Redis is recommended. |
| Prometheus Pushgateway | Yes | k6 and platform metric handoff. |
| Prometheus | Yes | k6 and platform time-series metrics. |
| InfluxDB | Yes | JMeter time-series metrics. |
| Grafana | Yes | Dashboard links from run detail pages. |
| Object storage | Recommended for shared deployments | Required when scripts, task assets, reports, or logs must be shared across hosts. |

## Optional Services

| Component | Default in local demo | When to add it |
| --- | --- | --- |
| Nacos | No | Add only for dynamic service discovery or Nacos-specific k6 extension demos. v0.1 agent dispatch does not require Nacos. |
| MinIO | No | Use MinIO when you need self-hosted S3-compatible object storage. Cloud S3-compatible storage is also valid. |
| Kafka | No | Add only as a test target or Kafka/xk6 extension demo. Kafka is not a control-plane dependency. |
| SkyWalking | No | Add only for APM integration. |
| Webhook notifications | No | Enable with `PTP_ENABLE_NOTIFICATIONS=true` and a bootstrap config file when you need PlanRun or threshold event callbacks. |
| Alertmanager | No | Add only after enabling alert ingestion and external alerting workflows. |
| Dynamic multi-agent scale-out | No | The local demo uses four static agents. Add more agents with static `AGENT_HOSTS`; validate scheduling and artifact storage before claiming scale-out support. |

## Single VM Baseline

For a small shared environment, a single Linux VM can start from the public compose stack with hardened configuration:

```bash
cp .env.example .env
```

In v0.1 alpha, `docker-compose.demo.yml` is the only maintained compose entrypoint. It is still a demo-oriented baseline, not a full production package. With demo credentials replaced, self-registration disabled, private networking, persistent backups, and HTTPS in front of the public services, it can be used as a single-VM starting point for small teams. A separate production compose or Kubernetes package should be treated as a later deployment artifact.

Edit `.env` before starting:

- change all demo passwords
- set `OPENLOADHUB_SECRET_KEY` to a long random value; the compose file maps it to the container runtime `SECRET_KEY`
- keep `ALLOW_SELF_REGISTER=0` unless you intentionally operate public registration
- set `PTP_PUBLIC_BASE_URL` and `GRAFANA_PUBLIC_BASE_URL` to externally reachable HTTPS URLs
- keep `OPENLOADHUB_DOCKER_CELERY_BROKER_URL` and `OPENLOADHUB_DOCKER_CELERY_RESULT_BACKEND` on the internal Redis service unless you are moving Redis to a managed endpoint
- set retention values for Prometheus, InfluxDB, reports, and logs

The v0.1 demo compose consumes these `.env` values directly for the app services:

- security and auth: `OPENLOADHUB_SECRET_KEY` mapped to runtime `SECRET_KEY`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_SECONDS`, `ALLOW_SELF_REGISTER`, default admin/tester account values
- public URLs: `PTP_PUBLIC_BASE_URL`, `GRAFANA_PUBLIC_BASE_URL`
- data plane: `OPENLOADHUB_DOCKER_DATABASE_URL`, `OPENLOADHUB_DOCKER_CELERY_BROKER_URL`, `OPENLOADHUB_DOCKER_CELERY_RESULT_BACKEND`
- observability: `OPENLOADHUB_DOCKER_PUSHGATEWAY_URL`, `OPENLOADHUB_DOCKER_PROMETHEUS_URL`, `OPENLOADHUB_DOCKER_GRAFANA_BASE_URL`, dashboard UIDs, InfluxDB and Grafana credentials
- product taxonomy: `BUSINESS_LINES`, `ENVIRONMENTS`, `OPENLOADHUB_DEMO_ENV`
- optional webhook bootstrap: `PTP_ENABLE_NOTIFICATIONS`, `NOTIFICATION_ENV_LABEL`, `PTP_WEBHOOK_BOOTSTRAP_FILE`
- dispatch and runtime: `OPENLOADHUB_DOCKER_AGENT_HOSTS`, `PTP_WORKER_CONCURRENCY`, `AGENT_INSTALL_CHROMIUM`, `JMETER_DOWNLOAD_URL`, `JMETER_GRPC_PLUGIN_URL`
- retention: `PROMETHEUS_RETENTION_TIME`, `INFLUXDB_RETENTION`, local artifact/report retention days

For a concrete fixed-agent example, see `demo/compose/fixed-four-agent.env.example` and `demo/compose/fixed-four-agent.override.example.yml`. They pin the same four static compose agents used by the local demo and keep task-level seed defaults separate from plan-level four-agent execution.

The seeded public demo tasks also carry default related-monitor links, a trace/topology link, prefilled script-variable metadata, `pod_count=1`, and `data_distribution=avg`. Demo plans can override node count during execution, but those task-level defaults are part of the out-of-the-box demo contract.

Public alpha quality hints are intentionally narrow and non-blocking:

- ScenarioQualityLint-lite warns about incomplete task or plan configuration, such as missing checks, missing observability links, missing script variables, ambiguous multi-pod data distribution, or obvious pod_count / TPS / VUs mismatch.

The first public alpha hides advanced analysis and report-review panels by default. Visible hints do not block creation, saving, or execution, and should not be presented as root-cause analysis or production acceptance.

Roadmap feature flags such as AI, mixed runs, self-APM, full alerting, trend analysis, and dynamic k6 control are pinned off by the public demo compose. Webhook notifications are supported as an optional integration, but remain off by default and require explicit configuration. Do not treat a `.env` toggle alone as support for the rest of the roadmap modules.

Then start:

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

Do not reuse the host mixed-mode env file for this full Docker command. The demo compose uses `OPENLOADHUB_DOCKER_DATABASE_URL`, `OPENLOADHUB_DOCKER_CELERY_BROKER_URL`, `OPENLOADHUB_DOCKER_CELERY_RESULT_BACKEND`, `OPENLOADHUB_DOCKER_PUSHGATEWAY_URL`, `OPENLOADHUB_DOCKER_PROMETHEUS_URL`, `OPENLOADHUB_DOCKER_GRAFANA_BASE_URL`, and `OPENLOADHUB_DOCKER_AGENT_HOSTS` for container-internal overrides. This keeps host-only values such as `127.0.0.1:13306` from leaking into containers.

Put a reverse proxy such as Nginx, Caddy, or a cloud load balancer in front of the UI, API, and Grafana. Terminate TLS at the proxy and restrict direct access to MySQL, Redis, InfluxDB, Prometheus, Pushgateway, and the agent port.

## Traditional Cloud Deployment

A typical VM-based cloud deployment uses:

- one or more application VMs for Web UI, Admin API, Worker, and Agent containers
- managed MySQL
- managed Redis
- cloud object storage or self-hosted MinIO
- Prometheus, InfluxDB, and Grafana as either platform-managed services or dedicated VMs
- HTTPS reverse proxy or cloud load balancer

Minimum environment settings for the app services:

```bash
DATABASE_URL=mysql+pymysql://openloadhub:<password>@<mysql-host>:3306/openloadhub
CELERY_BROKER_URL=redis://:<password>@<redis-host>:6379/0
CELERY_RESULT_BACKEND=redis://:<password>@<redis-host>:6379/1
AGENT_HOSTS=agent-a.example.com:9096,agent-b.example.com:9096
PUSHGATEWAY_URL=http://pushgateway.example.com:9091
PROMETHEUS_URL=http://prometheus.example.com:9090
GRAFANA_BASE_URL=http://grafana.example.com:3000
GRAFANA_PUBLIC_BASE_URL=https://grafana.example.com
PTP_PUBLIC_BASE_URL=https://openloadhub.example.com
ALLOW_SELF_REGISTER=0
SECRET_KEY=<long-random-secret>
```

If you adapt `docker-compose.demo.yml` directly for a single-node full Docker deployment, use the matching `OPENLOADHUB_DOCKER_*` names for those container-internal values.

For shared or multi-host artifact storage, enable S3-compatible storage on Admin API, Worker, and Agent:

```bash
USE_S3=1
AWS_ACCESS_KEY_ID=<access-key>
AWS_SECRET_ACCESS_KEY=<secret-key>
S3_BUCKET=openloadhub-artifacts
S3_REGION=us-east-1
S3_ENDPOINT=https://s3.example.com
S3_PUBLIC_ENDPOINT=https://s3.example.com
S3_PRESIGNED_ENDPOINT=https://s3.example.com
S3_RUN_ARTIFACT_PREFIX=runs
S3_REPORT_ARTIFACT_PREFIX=reports
```

Use a cloud object-storage service when available. Use MinIO when you need to self-host the S3-compatible endpoint. Do not use single-host Docker volumes as the only artifact store for a multi-host deployment.

## Runtime Configuration

Business lines and environments are configured through environment variables:

```bash
BUSINESS_LINES=demo:demo
ENVIRONMENTS=demo:demo:test,testnet:TestNet:testnet,mainnet:主网:main
OPENLOADHUB_DEMO_ENV=demo
```

`BUSINESS_LINES` accepts comma-separated `code:name` entries. `ENVIRONMENTS` accepts `code:name` or `code:name:scope`; scope can be `test`, `testnet`, or `main`. Task create/edit forms render those scopes as `test`, `testnet`, and `mainnet`. The seeded local demo tasks use `OPENLOADHUB_DEMO_ENV` and default to `demo`.

The public alpha uses built-in JWT users. `我的参与任务` filters tasks by `created_by` or `collaborator_ids`. User system integration is intentionally a small contract in v0.1 alpha, not a bundled SSO adapter.

## Webhook Notifications

Open-source builds can enable webhook notifications without editing private code paths. The recommended pattern is:

- keep `PTP_ENABLE_NOTIFICATIONS=false` by default
- commit a JSON bootstrap file without secrets
- reference secrets through `webhook_url_env` and `signing_secret_env`
- store the actual secrets in your runtime environment or secret manager

Example:

```bash
PTP_ENABLE_NOTIFICATIONS=true
NOTIFICATION_ENV_LABEL=prod
PTP_WEBHOOK_BOOTSTRAP_FILE=docs/public/examples/webhook-config.example.json
```

The example bootstrap file may include disabled entries for channels you do not use. Disabled entries without secrets are skipped during bootstrap and do not block startup. For the file format, template files, and API endpoints, see [Webhook Configuration](webhooks.md).

## User System Integration Contract

OIDC, LDAP, and SSO adapters are not included in v0.1 alpha. If you connect a company identity provider, use one of these extension shapes:

- gateway integration: terminate company auth at an ingress or API gateway, then map the authenticated principal to an OpenLoadHub user before requests reach the Admin API
- backend integration: extend the auth service to exchange company identity tokens for the built-in JWT and user record

Keep these platform fields stable:

- numeric user id used by task `created_by`, `owner`, audit fields, and collaborator mappings
- role code, currently `ADMIN` or `TESTER`
- username and display name shown in task and run records
- creator/collaborator mapping used by `我的参与任务`

When self-registration is disabled, initialize users through bootstrap env values, a maintainer script, a migration, or your identity sync job. Do not rewrite task ownership when syncing users; preserve the existing numeric user ids or provide an explicit migration.

中文：业务线、环境、demo seed 使用的环境都从 `.env` 读取。接入自有用户系统时，需要保持稳定用户 ID、角色和任务 owner/collaborator 映射；v0.1 alpha 暂不内置 OIDC / LDAP / SSO 适配器。

## Agent Dispatch

Public v0.1 uses static agent hosts:

```bash
AGENT_HOSTS=ptp-agent:9096,ptp-agent-2:9096,ptp-agent-3:9096,ptp-agent-4:9096
```

For more than one agent:

```bash
AGENT_HOSTS=agent-a.example.com:9096,agent-b.example.com:9096
```

Nacos is not required for this dispatch path. Add Nacos only when you intentionally build dynamic discovery or Nacos-specific protocol demos around it.

## Security Checklist

- replace every demo credential before shared use
- set a strong `OPENLOADHUB_SECRET_KEY` for demo compose, or runtime `SECRET_KEY` when deploying app services outside that compose file
- keep self-registration disabled unless there is an explicit onboarding policy
- run UI, API, and Grafana behind HTTPS
- keep databases, Redis, InfluxDB, Prometheus, Pushgateway, object storage, and agents on private networks
- give object-storage credentials least-privilege bucket access
- enable MySQL, Redis, object-storage, InfluxDB, Prometheus, and Grafana backups according to your retention policy
- keep public alpha feature flags disabled for roadmap-only features such as mixed runs, alerts, self APM, trend analysis, AI features, and dynamic k6 control

## Post-Deployment Smoke

After deployment:

1. open the UI and log in with a non-demo admin account
2. create or seed one tester account
3. run one small HTTP task against a safe target
4. confirm RunDetail status, logs, report links, and dashboard links
5. confirm Prometheus, InfluxDB, and Grafana show fresh data
6. if `USE_S3=1`, confirm logs/reports can be restored from object storage
7. if multiple agents are configured, run at least one task per agent and verify artifact paths and metrics are separated by run id

Do not expose a shared deployment before these checks pass.
