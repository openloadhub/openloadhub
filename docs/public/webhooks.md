# Webhook Configuration

Webhook notifications are available in the open-source build, but disabled by default in the demo compose. The platform supports:

- `plan_run_completed`
- `plan_run_failed`
- `threshold_breached`
- `regression_blocked`

Supported channels:

- `feishu`
- `wecom`
- `dingtalk`

## What Is Configurable

Webhook configs are still stored in the database at runtime because the worker needs the final channel, URL, template, retry, and signing settings to send notifications. To make this reproducible for anyone who clones the repository, the open-source build now supports a repo-managed bootstrap file:

- config file: `PTP_WEBHOOK_BOOTSTRAP_FILE`
- inline template: `template`
- file-based template: `template_file`
- secret indirection: `webhook_url_env`, `signing_secret_env`

The bootstrap file is JSON and can be committed without secrets. Secrets stay in your local `.env`, shell environment, or deployment secret manager.

## Quick Start

1. Enable notifications in `.env`:

```bash
PTP_ENABLE_NOTIFICATIONS=true
NOTIFICATION_ENV_LABEL=demo
PTP_WEBHOOK_BOOTSTRAP_FILE=docs/public/examples/webhook-config.example.json
```

2. Export secrets before starting or recreating the stack:

```bash
export OPENLOADHUB_FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
export OPENLOADHUB_FEISHU_SIGNING_SECRET='replace-me-if-feishu-signing-is-enabled'
export OPENLOADHUB_WECOM_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'
```

3. Start or recreate the demo stack:

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

When `ptp-admin` starts, it reads `PTP_WEBHOOK_BOOTSTRAP_FILE` and upserts configs into `olh_webhook_config` by `name`.

## Example Bootstrap File

See:

- [webhook-config.example.json](examples/webhook-config.example.json)
- [plan-run-terminal.md](examples/templates/plan-run-terminal.md)
- [threshold-breached.md](examples/templates/threshold-breached.md)

The example uses:

- `webhook_url_env` instead of a hard-coded URL
- `signing_secret_env` for Feishu signing
- `template_file` so the message body can be edited in the repo instead of only in the database
- disabled channels can stay in the same file; if their secrets are unset, bootstrap skips them instead of failing startup

## Supported Fields

Each bootstrap entry accepts the same main fields as the API:

- `name`
- `channel`
- `event_types`
- `enabled`
- `title`
- `template` or `template_file`
- `webhook_url` or `webhook_url_env`
- `signature_type`
- `signing_secret` or `signing_secret_env`
- `timeout_seconds`
- `max_retry_count`
- `retry_interval_seconds`

## Template Variables

Common PlanRun variables:

- `plan_name`
- `plan_run_id`
- `plan_id`
- `status_result_label`
- `plan_exec_type_label`
- `round_label`
- `execution_summary_line`
- `task_run_summary`
- `metrics_summary_line`
- `status_detail_summary`
- `plan_run_url`
- `notification_env`

Threshold-event variables:

- `alertname`
- `severity_label`
- `alert_status_label`
- `source`
- `subscription_label`
- `target_label`
- `alert_summary`
- `alert_reason`
- `action_status_label`
- `alert_url`

## API Endpoints

When `PTP_ENABLE_NOTIFICATIONS=true`, these APIs are available:

- `POST /api/v1/notifications/webhook/configs`
- `GET /api/v1/notifications/webhook/configs`
- `PUT /api/v1/notifications/webhook/configs/{config_id}`
- `POST /api/v1/notifications/webhook/preview`
- `POST /api/v1/notifications/webhook/send`
- `GET /api/v1/notifications/webhook/records`

These endpoints require `notification.manage`, which is currently granted to `admin` and `manager`.

## Notes

- If `PTP_ENABLE_NOTIFICATIONS=false`, the webhook API is hidden and automatic terminal notifications are skipped.
- `ptp-worker` needs the same `PTP_PUBLIC_BASE_URL` and `NOTIFICATION_ENV_LABEL` values as `ptp-admin`, because automatic PlanRun notifications are sent from the worker execution path.
- Editing the repo template file and recreating `ptp-admin` is enough to upsert the latest template into the database.
- For the example bootstrap file, only enabled channels must provide real webhook secrets. Disabled channels without secrets are ignored during bootstrap.
- Feishu bots may enable keyword verification. In that mode, Feishu returns HTTP `200` but business code `19024` / `Key Words Not Found` when the rendered title/body does not contain the bot keyword. The platform records this as a failed send. Add the configured keyword to the webhook `title` or template, or disable keyword verification on the Feishu bot.
