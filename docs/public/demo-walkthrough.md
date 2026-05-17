# Demo Walkthrough

This walkthrough shows the first path a new evaluator should try after starting the local demo.

## 1. Start And Sign In

Start the stack from the repository root:

```bash
cp .env.example .env
docker compose -f docker-compose.demo.yml up -d --build
```

Open `http://127.0.0.1:13000` and sign in as:

- username: `demo_tester`
- password: `ptp_demo_tester`

## 2. Run The k6 Demo Task

Open `关注任务`, then choose `OpenLoadHub Demo - k6 HTTP+gRPC`.

Start a run with the default parameters. The demo targets the local HTTP and gRPC service included in the Compose stack.

After the run finishes, open RunDetail and check:

- summary metrics
- endpoint trends
- checks
- logs
- generated report
- Grafana dashboard links

## 3. Run The JMeter Demo Task

Return to `关注任务`, then choose `OpenLoadHub Demo - JMeter HTTP+gRPC`.

Start a run with the default parameters. After completion, open RunDetail and inspect:

- JMeter summary rows
- endpoint trend chart
- assertions/check results
- logs and report
- InfluxDB/Grafana links

## 4. Try A Demo Plan

Open `批次模板` and run one of the seeded demo plans:

- `OpenLoadHub Demo Plan - JMeter Simple`
- `OpenLoadHub Demo Plan - k6 + JMeter Advanced`

Use PlanRun detail to inspect linked runs and reports.

## 5. Capture First-Run Evidence

If you are validating a fresh local demo or preparing a bug report, capture:

- the RunDetail header for the completed k6 run
- the RunDetail header for the completed JMeter run
- one generated report link
- one Grafana dashboard link
- `docker compose -f docker-compose.demo.yml ps`

If any of these are missing or stale, use [Troubleshooting](troubleshooting.md) before treating the demo as broken.

## 6. Screenshot Map

Use this map when you want a compact first-run evidence pack. Keep the browser URL visible when it is a local demo URL, and redact browser profiles or private tabs.

| Step | Suggested file name | What should be visible |
| --- | --- | --- |
| Sign-in/frontdoor | `01-login-or-focus-task.png` | `http://127.0.0.1:13000`, signed-in user, and the `关注任务` entry or page |
| k6 RunDetail | `02-k6-rundetail.png` | run id, engine type `k6`, terminal status, summary metrics, and endpoint trends |
| JMeter RunDetail | `03-jmeter-rundetail.png` | run id, engine type `jmeter`, terminal status, summary rows, and endpoint trends |
| PlanRun detail | `04-planrun-detail.png` | plan run id, linked child run ids, engine types, and terminal statuses |
| Grafana/report link | `05-report-or-grafana.png` | the report or Grafana URL opened from RunDetail, with run window or run variable visible when present |
| Compose state | `06-compose-ps.txt` | output of `docker compose -f docker-compose.demo.yml ps` |

Do not include secrets, private endpoints, customer data, or screenshots from internal infrastructure. For symptoms that need logs, use the matching section in [Troubleshooting](troubleshooting.md).

## 7. What This Proves

The walkthrough proves that the local control plane can create and run seeded JMeter/k6 work, collect results, expose logs and reports, and link to local observability dashboards.

It does not prove production high availability, dynamic agent discovery, enterprise identity, or full observability integration for your own infrastructure.
