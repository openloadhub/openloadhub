# Troubleshooting

Use this page when the local demo starts but the first run does not reach a useful RunDetail page. For the happy path, start with [Quickstart](quickstart.md) and [Demo Walkthrough](demo-walkthrough.md).

中文说明：本页用于本地 demo 首次启动后的常见排障。正常路径请先看 [quickstart.md](quickstart.md) 和 [demo-walkthrough.md](demo-walkthrough.md)。

## First Checks

Run these commands from the repository root:

```bash
docker compose -f docker-compose.demo.yml ps
curl -fsS http://127.0.0.1:18000/health
docker compose -f docker-compose.demo.yml logs --tail=120 ptp-admin ptp-worker ptp-demo-seed
docker compose -f docker-compose.demo.yml logs --tail=80 ptp-agent ptp-agent-2 ptp-agent-3 ptp-agent-4
```

The expected local demo shape is:

- UI on `http://127.0.0.1:13000`
- API on `http://127.0.0.1:18000`
- Grafana on `http://127.0.0.1:13001`
- Prometheus on `http://127.0.0.1:19090`
- four static Docker agents: `ptp-agent`, `ptp-agent-2`, `ptp-agent-3`, and `ptp-agent-4`
- seeded tasks and plans created by the one-shot `ptp-demo-seed` container

中文：默认本地 demo 是固定四个 Docker agent，不是动态发现。首次列表为空时，优先看 `ptp-demo-seed` 日志是否出现 `seed_complete`。

If the UI does not open, check whether the expected ports are already used by another local stack:

```bash
lsof -iTCP:13000 -sTCP:LISTEN
lsof -iTCP:18000 -sTCP:LISTEN
lsof -iTCP:13001 -sTCP:LISTEN
```

Stop the conflicting local service or change its port before restarting the demo. Keep the OpenLoadHub demo ports unchanged for the first walkthrough so links in RunDetail and Grafana match this documentation.

## Seeded Tasks Or Plans Are Missing

If `关注任务` or `批次模板` is empty after sign-in:

```bash
docker compose -f docker-compose.demo.yml logs ptp-demo-seed
```

Look for `seed_complete`. If the seed container has not finished, wait and refresh the page. If it failed, capture the seed log and the admin API log together:

```bash
docker compose -f docker-compose.demo.yml logs --tail=200 ptp-demo-seed ptp-admin
```

Sign in as `demo_tester` / `ptp_demo_tester` for the first walkthrough. The bootstrap `admin` account is also added as a collaborator on the seeded demo content, but `demo_tester` is the simplest first-run account.

## Runs Stay Queued Or Preparing

The demo worker dispatches to the four static Compose agents. Check that the worker and all four agents are running:

```bash
docker compose -f docker-compose.demo.yml ps ptp-worker ptp-agent ptp-agent-2 ptp-agent-3 ptp-agent-4
docker compose -f docker-compose.demo.yml logs --tail=160 ptp-worker
```

If you customized `.env`, make sure full Docker overrides use the container-internal host list:

```bash
OPENLOADHUB_DOCKER_AGENT_HOSTS=ptp-agent:9096,ptp-agent-2:9096,ptp-agent-3:9096,ptp-agent-4:9096
```

Do not use host-only addresses such as `127.0.0.1:19096` inside `OPENLOADHUB_DOCKER_AGENT_HOSTS`; containers must call the Compose service names.

## Report Or Grafana Links Do Not Open

RunDetail links use the public base URLs from the demo environment. For the default local stack, open:

- OpenLoadHub UI: `http://127.0.0.1:13000`
- Grafana: `http://127.0.0.1:13001`
- Prometheus: `http://127.0.0.1:19090`

If Grafana opens but a dashboard has no data, confirm the run completed and then check the relevant metrics services:

```bash
docker compose -f docker-compose.demo.yml ps prometheus pushgateway influxdb grafana
docker compose -f docker-compose.demo.yml logs --tail=120 prometheus pushgateway influxdb grafana
```

The demo includes local dashboards for the seeded JMeter and k6 paths. A SkyWalking trace/topology URL may appear as an integration placeholder; it is only live if you attach a matching SkyWalking deployment yourself.

If a report link returns 404 immediately after a run completes, wait a few seconds and refresh once. If it still fails, capture the RunDetail header, the report URL, and recent app logs:

```bash
docker compose -f docker-compose.demo.yml logs --tail=160 ptp-admin ptp-worker ptp-agent ptp-agent-2 ptp-agent-3 ptp-agent-4
```

## Browser Shows An Old Page

After rebuilding the frontend image, hard refresh the browser tab. If the page still looks stale, recreate the frontend container:

```bash
docker compose -f docker-compose.demo.yml up -d --build frontend
```

Keep the compose project running unless you intentionally want to reset local data. Use `down -v` only when you want to delete the demo database and volumes.

## Docker Storage Pressure

If login requests time out, Redis reports `MISCONF`, or services are `Up` but background work stops, check Docker Desktop storage:

```bash
docker system df
docker exec "$(docker compose -f docker-compose.demo.yml ps -q redis)" df -h /data
docker exec "$(docker compose -f docker-compose.demo.yml ps -q redis)" redis-cli INFO persistence
```

Safe cleanup steps that keep demo database volumes:

```bash
docker builder prune -af
docker image prune -af
```

## Useful Screenshots For Bug Reports

For a reproducible public alpha bug report, attach sanitized screenshots of:

- `docker compose -f docker-compose.demo.yml ps`
- the task or plan page where you clicked Run
- the RunDetail header with status, engine type, and run id visible
- the report or Grafana link that failed, if that is the problem

Also include the OpenLoadHub commit, browser version, Docker version, and the minimal logs listed in the section that matches your symptom. Do not include secrets, private endpoints, customer data, or internal infrastructure screenshots.

中文：提交问题时请附上可复现步骤、当前 commit、浏览器和 Docker 版本，以及对应日志。截图需要脱敏，不要包含密钥、客户数据或内部地址。

Good screenshot rules:

- keep the browser URL visible when it is a local demo URL
- keep the run id, engine type, and status visible on RunDetail
- crop out browser profiles, tokens, private tabs, customer names, and private hostnames
- prefer PNG screenshots over photos of a screen
- name files by symptom, for example `rundetail-report-404.png` or `empty-task-list-after-seed.png`

For a full first-run evidence pack, use the screenshot map in [Demo Walkthrough](demo-walkthrough.md#6-screenshot-map). It lists the smallest useful set of pages and suggested file names.

If you cannot share screenshots, include the same facts as text: page name, action clicked, visible status, run id if present, and the exact local URL that failed.
