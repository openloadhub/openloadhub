# Task Script Examples

These examples are small starting points for the public alpha script upload flow. They target the local demo service from `docker-compose.demo.yml`.

Use the seeded demo tasks first when evaluating OpenLoadHub. Use these files when you want to create a new task from an uploaded script and keep the target contract simple.

## k6 HTTP Smoke

- file: `k6-http-smoke.js`
- default target: `http://demo-target:8080`
- useful variables:
  - `BASE_URL`: target base URL
  - `DURATION`: k6 duration, for example `20s`
  - `VUS`: virtual users

## JMeter HTTP Smoke

- file: `jmeter-http-smoke.jmx`
- default target: `demo-target:8080`
- useful JMeter properties:
  - `target_protocol`
  - `target_host`
  - `target_port`
  - `target_path`
  - `threads`
  - `loops`

## Boundaries

These examples do not enable dynamic k6 TPS, dynamic agent discovery, mixed-run UX, enterprise identity, or production high availability. They are intentionally small scripts for first-run task creation and troubleshooting.
