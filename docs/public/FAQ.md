# FAQ

## Can OpenLoadHub be used commercially?

Yes. The OpenLoadHub platform source, public documentation, demo Compose files, and Docker build wiring are released under Apache-2.0. You may use them for commercial and internal business purposes under that license.

This does not relicense third-party runtimes. k6 is AGPL-3.0, Apache JMeter is Apache-2.0, and any optional plugins or forks keep their own licenses. See `source-and-license.md` before redistributing runtime images or modified binaries.

## Is v0.1 alpha production safe?

Treat v0.1 alpha as a self-hosted evaluation and small-team starting point, not a turnkey production platform. The local demo proves the core JMeter and k6 workflow, reports, logs, and Grafana links. Shared deployments still need HTTPS, secret rotation, private networks, backups, object storage, and environment-specific validation.

## How is this different from using JMeter, k6, or Grafana directly?

OpenLoadHub is not a replacement for those tools. It is a control plane around them:

- task, script, run, report, log, and dashboard workflow in one UI
- JMeter and k6 under one run model
- seeded demo tasks and plans for quick local evaluation
- RunDetail pages that keep result, report, log, and observability links together

If your team only needs a single CLI test and already has all reporting solved, direct JMeter or k6 may be simpler.

## Why are mixed runs, trend analysis, Self-APM, and dynamic k6 TPS not fully enabled?

The public alpha keeps the default workflow narrow so new users can understand and run it. These capabilities need stronger UX, source disclosure, runtime proof, or deployment docs before they should be presented as supported public features.

Dynamic k6 TPS is planned for v0.2 after the OpenLoadHub k6 source, AGPL-compatible license notice, tag, build command, and reproducible runtime proof are public.

## Will the project become closed source later?

Published Apache-2.0 releases remain open under Apache-2.0. Future cloud services, enterprise deployment features, commercial plugins, or support packages may be offered separately under an open-core model, but released community source is not retroactively closed.

## How fast are issues handled?

The public alpha is maintained on a best-effort basis. Reproducible bugs with logs, screenshots, Docker version, browser version, and the OpenLoadHub commit are the easiest to triage. Security issues should not be posted as public issues; use the private vulnerability reporting channel described in `SECURITY.md`.

## What belongs in a feature request?

Good feature requests describe the user problem, the first useful version, and what should stay out of scope. Requests that require large deployment architecture, enterprise identity, custom plugins, or paid operational help may be routed to commercial support instead of community support.

## What should I try first?

Start the local demo, sign in as `demo_tester`, run one seeded k6 task and one seeded JMeter task, then inspect RunDetail, the generated report, logs, and Grafana links. The demo walkthrough explains that path.
