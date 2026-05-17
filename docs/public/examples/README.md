# Examples

This directory contains public examples that are safe to copy and adapt.

## Environment

- `openloadhub-host.env.example`: host mixed-mode environment for local debugging while demo middleware and Docker agents stay online.

## Webhooks

- `webhook-config.example.json`: webhook bootstrap file with secret indirection.
- `templates/plan-run-terminal.md`: PlanRun terminal notification template.
- `templates/threshold-breached.md`: threshold notification template.

## Task Scripts

- `task-scripts/k6-http-smoke.js`: minimal k6 HTTP smoke script for the local demo target.
- `task-scripts/jmeter-http-smoke.jmx`: minimal JMeter HTTP smoke test plan for the local demo target.

## Demo Target

The local demo target source lives in `demo/target-service/`. It provides stable HTTP and gRPC endpoints for the seeded demo tasks.

## Scripts And Smoke Checks

- `scripts/run-opensource-demo-smoke.py`: verifies the public demo stack and core endpoints.
- `scripts/seed-public-demo.py`: creates the seeded demo tasks and plans.

Keep examples free of secrets, private endpoints, customer data, and environment-specific assumptions.
