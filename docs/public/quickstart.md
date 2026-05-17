# Quickstart

Use this guide to start the OpenLoadHub local demo from the repository root and reach the first useful RunDetail page in about 15 minutes after images are available locally.

中文说明：本指南用于从仓库根目录启动 OpenLoadHub 本地 demo。

Shared or traditional cloud deployments are covered in [Deployment Guide](deployment.md).

## 15 Minute Path

1. Copy the local environment template:

```bash
cp .env.example .env
```

2. Start the full Docker demo:

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

3. Open `http://127.0.0.1:13000` and sign in as `demo_tester` / `ptp_demo_tester`.
4. Open `关注任务`.
5. Run `OpenLoadHub Demo - k6 HTTP+gRPC`.
6. Open the completed run's RunDetail page.
7. Inspect summary metrics, checks, logs, report, and Grafana links.
8. Run `OpenLoadHub Demo - JMeter HTTP+gRPC` and compare the JMeter result shape.

For a page-by-page first run, see [Demo Walkthrough](demo-walkthrough.md). If the task list, run status, report, or Grafana links do not match the expected path, use [Troubleshooting](troubleshooting.md).

中文快速路径：复制 `.env.example`，启动 `docker-compose.demo.yml`，用 `demo_tester` 登录，从 `关注任务` 运行内置 k6 / JMeter demo task，然后进入 RunDetail 查看指标、日志、报告和 Grafana 链接。首次页面路径见 [Demo Walkthrough](demo-walkthrough.md)；遇到列表为空、运行卡住、报告或 Grafana 链接打不开时看 [Troubleshooting](troubleshooting.md)。

## Requirements

- Docker with Compose support
- 8 GB RAM or more recommended
- 15 GB or more free space in Docker Desktop's disk image recommended
- ports `13000`, `18000`, `13001`, `19090`, `18086`, `19091`, `19096`, `19097`, `19098`, `19099`, `18088`, `15051`, `13306`, and `16379` available

中文要求：

- 已安装支持 Compose 的 Docker
- 建议至少 8 GB 内存
- 建议 Docker Desktop 磁盘镜像至少保留 15 GB 可用空间
- 本机端口 `13000`、`18000`、`13001`、`19090`、`18086`、`19091`、`19096`、`19097`、`19098`、`19099`、`18088`、`15051`、`13306`、`16379` 未被占用

Optional local overrides:

```bash
cp .env.example .env
```

The default public demo MySQL port is `13306`. Keep it separate from any other local stack that may use `3306`.

## Start The Demo

From the repository root:

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

中文：在仓库根目录执行以上命令启动 demo。

Do not pass `docs/public/examples/openloadhub-host.env.example` to this full Docker command. That file is only for host mixed-mode. The demo compose intentionally uses container-internal defaults for MySQL, Redis, Prometheus, Grafana, and agents; custom full Docker overrides should use `OPENLOADHUB_DOCKER_*` variables.

中文：full Docker 启动不要加载 `docs/public/examples/openloadhub-host.env.example`。该文件只用于 host mixed-mode。full Docker 的容器内连接地址由 compose 默认值提供；如需覆盖，使用 `OPENLOADHUB_DOCKER_*` 变量。

If you want an explicit example of the default fixed four-agent shape, use the optional compose templates:

```bash
docker compose --env-file demo/compose/fixed-four-agent.env.example \
  -f docker-compose.demo.yml \
  -f demo/compose/fixed-four-agent.override.example.yml \
  up -d --build
```

The templates keep four static agents and seed demo tasks with `pod_count=1` while demo plans run with `pod_count=4`. They are examples of the default local v0.1 alpha topology, not dynamic discovery or production HA.

After the command returns, confirm that the main services are up:

```bash
docker compose -f docker-compose.demo.yml ps
curl -fsS http://127.0.0.1:18000/health
docker compose -f docker-compose.demo.yml logs ptp-demo-seed
```

`ptp-demo-seed` is expected to exit after seeding the demo accounts, tasks, and plans. Look for `seed_complete` before treating an empty task or plan list as a failure.

中文：启动后先检查 compose 服务列表、API health 和 `ptp-demo-seed` 日志。`ptp-demo-seed` 是一次性容器，完成 seed 后退出属于预期；看到 `seed_complete` 后再判断页面数据是否异常。

Open:

- UI: `http://127.0.0.1:13000`
- API: `http://127.0.0.1:18000`
- Grafana: `http://127.0.0.1:13001`
- Prometheus: `http://127.0.0.1:19090`
- InfluxDB: `http://127.0.0.1:18086`
- Demo target HTTP: `http://127.0.0.1:18088`
- Demo target gRPC: `127.0.0.1:15051`
- Agent 1: `127.0.0.1:19096`
- Agent 2: `127.0.0.1:19097`
- Agent 3: `127.0.0.1:19098`
- Agent 4: `127.0.0.1:19099`

Default demo credentials:

- OpenLoadHub admin: `admin` / `ptp_demo_admin`
- OpenLoadHub tester: `demo_tester` / `ptp_demo_tester`
- Grafana: `admin` / `ptp_demo_grafana`

These are local demo defaults and must be changed before any shared deployment.

中文：以上账号仅用于本地 demo，部署到共享环境前必须修改；默认不开放自注册。

Seeded demo content:

- `OpenLoadHub Demo - k6 HTTP+gRPC`
- `OpenLoadHub Demo - JMeter HTTP+gRPC`
- `OpenLoadHub Demo Plan - JMeter Simple`
- `OpenLoadHub Demo Plan - k6 + JMeter Advanced`

Default seeded task contract:

- each demo task includes 3 related monitor entries
- each demo task includes 1 trace / topology entry
- each demo task pre-fills script variables, variable types, and variable descriptions
- each demo task defaults to `pod_count=1`
- each demo task defaults to `data_distribution=avg`
- engine-specific metadata stays intact, while execution controls such as k6 iteration counts, VUs, and duration belong to the run strategy instead of long-lived script variables
- demo plans may override task-level node count during execution

Lightweight configuration hints in the demo:

- Task and plan detail pages may show ScenarioQualityLint-lite warnings. These warnings are non-blocking configuration lint hints and only point out missing checks/assertions, monitors, trace/topology links, script variables, data distribution, or obvious pod_count / TPS / VUs mismatch.

The first public alpha intentionally hides advanced analysis and report-review panels by default. The demo still exposes core result details, logs, reports, Grafana dashboard links, and the lightweight configuration lint hints above. Those visible lint hints are meant to guide manual review; they are not automatic acceptance, root-cause analysis, or production conclusions.

The seed job is a one-shot container. If the task or plan list is still empty right after `up -d`, wait for the seed log:

```bash
docker compose -f docker-compose.demo.yml logs ptp-demo-seed
```

Look for `seed_complete`, then log in as `demo_tester` and open `关注任务` or `批次模板`. The seed keeps `demo_tester` as the creator of the demo tasks and demo plans, and also adds the bootstrap `admin` account as a collaborator, so `admin` can see the same demo content with the default `我的参与任务` / `我的模板` filter.

中文：首次启动后会自动生成以上两条任务和两条批次模板。如果刚打开页面时列表为空，先查看 `ptp-demo-seed` 日志，看到 `seed_complete` 后再登录 `demo_tester` 进入 `关注任务` 或 `批次模板`。seed 会保留 `demo_tester` 作为创建人，并把内置 `admin` 账号加入协作人，所以 admin 默认选择 `我的参与任务` / `我的模板` 时也能看到同一组 demo 内容。可以直接试跑，也可以复制后修改目标服务和参数。

## Optional Webhook Notifications

Skip this section for a normal first run. Webhooks are optional and disabled by default.

```bash
export OPENLOADHUB_FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
export OPENLOADHUB_FEISHU_SIGNING_SECRET='replace-me-if-needed'
```

Then edit `.env` if you want webhook notifications:

```bash
PTP_ENABLE_NOTIFICATIONS=true
NOTIFICATION_ENV_LABEL=demo
PTP_WEBHOOK_BOOTSTRAP_FILE=docs/public/examples/webhook-config.example.json
```

See [Webhook Configuration](webhooks.md) for the full setup and template workflow.

## Optional Task Script Examples

Skip this section for the normal seeded first run. If you want to create a fresh task from an uploaded script, start with the public examples:

- `docs/public/examples/task-scripts/k6-http-smoke.js`
- `docs/public/examples/task-scripts/jmeter-http-smoke.jmx`

They target the same local demo service used by the seeded tasks and keep the variable contract small. See [Examples](examples/README.md) for the file list and boundaries.

## Local Data Isolation

The compose project uses its own Docker volumes. Data created by this demo is separate from any other local development stack. For example, another stack may use `http://127.0.0.1:3000` / `http://127.0.0.1:8000`; that data will not appear in the public demo stack on `http://127.0.0.1:13000` / `http://127.0.0.1:18000`.

中文：本地 demo 使用独立 Docker volume。不同 compose project / 不同端口上的历史开发数据不会自动显示在当前 demo 中；这属于数据隔离，不代表旧数据被删除。

## Host Mixed-Mode For Public Demo

Skip this section for a normal first run. It is only for faster local debugging after the full Docker demo path is understood.

For faster local debugging on macOS, you can run `ptp-admin`, `ptp-worker`, and the frontend on the host while keeping the public demo middleware, demo target, and four Docker agents online. Do not use `.env.host.local` for this public demo mode; that file is usually for a different local stack and may point at `127.0.0.1:3306` / `6379`.

```bash
docker compose -f docker-compose.demo.yml stop frontend ptp-admin ptp-worker
ENV_FILE=docs/public/examples/openloadhub-host.env.example ./scripts/start-host-apps.sh ptp-admin
ENV_FILE=docs/public/examples/openloadhub-host.env.example ./scripts/start-host-apps.sh ptp-worker
ENV_FILE=docs/public/examples/openloadhub-host.env.example ./scripts/start-host-apps.sh frontend
```

This profile keeps the public demo ports: host UI `13000`, host API `18000`, MySQL `13306`, Redis `16379`, Grafana `13001`, Prometheus `19090`, Pushgateway `19091`, and Docker agents `19096` through `19099`.

中文：OpenLoadHub public demo 的 host mixed-mode 必须显式使用 `docs/public/examples/openloadhub-host.env.example` 或由它派生的本地环境文件，避免误连其他本地数据库。

## Configure Environments And Business Lines

`BUSINESS_LINES` and `ENVIRONMENTS` are read from `.env` and can be changed before starting the stack:

```bash
BUSINESS_LINES=demo:demo
ENVIRONMENTS=demo:demo:test,testnet:TestNet:testnet,mainnet:主网:main
OPENLOADHUB_DEMO_ENV=demo
```

Use `code:name` for business lines. Use `code:name:scope` for environments when you want tabs and task forms to classify an environment as `test`, `testnet`, or `main`; missing scope defaults to `test`. The task create/edit form displays the selected environment type as `test`, `testnet`, or `mainnet`.

After changing `.env`, recreate the app services:

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

中文：业务线和环境都来自 `.env`。默认暴露 `demo / testnet / mainnet` 三个压测环境，创建任务页选择 `testnet/mainnet` 后环境类型会显示对应的 `testnet/mainnet`。修改后重新执行 compose 启动命令即可。内置 demo 任务默认使用 `OPENLOADHUB_DEMO_ENV=demo`。

## User System Integration

The public alpha ships with built-in JWT login and bootstrapped `admin` / `demo_tester` accounts. Self-registration is disabled by default. If you connect your own user system later, keep a stable numeric user id, role mapping, and task ownership/collaborator mapping so `我的参与任务` can continue filtering by creator or collaborator. See [Deployment Guide](deployment.md).

中文：当前版本没有内置 OIDC / LDAP / SSO 适配器。接入企业用户体系时，建议按 Deployment Guide 的用户系统接入合约，在网关或后端扩展中映射稳定用户 ID、角色和任务创建人/协作人关系；平台任务列表的 `我的参与任务` 过滤依赖这些字段。

## Stop The Demo

```bash
docker compose -f docker-compose.demo.yml down
```

中文：停止 demo 使用以上命令。

To remove local demo volumes:

```bash
docker compose -f docker-compose.demo.yml down -v
```

中文：如果需要同时删除本地 demo 数据卷，使用以上 `down -v` 命令。

## Troubleshooting Local Storage

For seeded task, run dispatch, report, Grafana, and screenshot evidence guidance, see [Troubleshooting](troubleshooting.md).

If login requests time out, Redis reports `MISCONF`, or background tasks stop while containers still show `Up`, check Docker Desktop disk usage first. The macOS host filesystem can still have free space while the Docker VM disk image is full.

Useful checks:

```bash
docker system df
docker compose -f docker-compose.demo.yml ps
docker exec "$(docker compose -f docker-compose.demo.yml ps -q redis)" df -h /data
docker exec "$(docker compose -f docker-compose.demo.yml ps -q redis)" redis-cli INFO persistence
```

Safe first cleanup steps that do not delete demo database volumes:

```bash
docker builder prune -af
docker image prune -af
```

Do not run `docker compose -f docker-compose.demo.yml down -v` unless you intentionally want to delete the local demo data.

## First-Run Evidence Checklist

When you are validating the demo for yourself or preparing a public bug report, save:

- `docker compose -f docker-compose.demo.yml ps`
- `docker compose -f docker-compose.demo.yml logs --tail=120 ptp-admin ptp-worker ptp-demo-seed`
- a screenshot of the task or plan page you used to start the run
- a screenshot of the RunDetail header with status, engine type, and run id visible
- the report or Grafana URL that failed, if the problem is about a link or dashboard

Do not include secrets, private endpoints, customer data, or screenshots from internal infrastructure. More screenshot guidance is in [Troubleshooting](troubleshooting.md#useful-screenshots-for-bug-reports).

## Current Alpha Caveats

- The public demo compose excludes Nacos, Kafka, SkyWalking, Alertmanager, MinIO, and dynamic agent discovery.
- Demo tasks include a SkyWalking trace/frontdoor URL for integration shape; it only becomes a live trace view when you run an external or optional SkyWalking stack at that address.
- Local agent dispatch uses four static compose agents by default. For full Docker overrides, use `OPENLOADHUB_DOCKER_AGENT_HOSTS=ptp-agent:9096,ptp-agent-2:9096,ptp-agent-3:9096,ptp-agent-4:9096`; Nacos is not required for the v0.1 demo path.
- Local scripts and task assets use Docker volumes. Shared or multi-host deployments should use S3-compatible object storage or MinIO.
- Dynamic k6 rate control is planned for v0.2 after the public OpenLoadHub k6 source and build proof are complete.
- The agent image must not rely on committed custom k6 binaries in the public repository.
- Webhook notifications are available but disabled by default; enable them explicitly through `.env` plus a bootstrap config file.

中文注意事项：

- public demo compose 不包含 Nacos、Kafka、SkyWalking、Alertmanager、MinIO 和动态 agent 发现。
- demo 任务会预置 SkyWalking 关联链路/frontdoor URL，用于展示集成形态；只有你额外启动或接入该地址的 SkyWalking 时，它才代表真实可打开的 trace 视图。
- 本地 agent 调度默认使用四台静态 compose agent。full Docker 如需覆盖，使用 `OPENLOADHUB_DOCKER_AGENT_HOSTS=ptp-agent:9096,ptp-agent-2:9096,ptp-agent-3:9096,ptp-agent-4:9096`；v0.1 demo 链路不依赖 Nacos。
- 本地脚本和任务资产使用 Docker volume；共享或多主机部署应使用 S3-compatible object storage 或 MinIO。
- k6 动态 TPS 调整计划在 v0.2 提供，前提是公开 OpenLoadHub k6 源码和可复现构建证明完成。
- public 仓库中的 agent 镜像不能依赖已提交的自定义 k6 binary。
