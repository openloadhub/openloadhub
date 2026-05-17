# Known Limitations

OpenLoadHub v0.1 alpha intentionally keeps the public surface small.

## Runtime And Deployment

- The default demo uses four static Docker agents. It is not dynamic discovery, autoscaling, or high availability.
- The demo Compose file is for local evaluation and small-team starting points. It is not a hardened production package.
- First-run screenshots, logs, and Compose output are diagnostic evidence only. They do not replace environment-specific production validation.
- Shared deployments need HTTPS, secret rotation, private networks, backups, and object storage validation.
- Local Docker volumes are suitable for the demo. Multi-host deployments should use S3-compatible object storage or MinIO.
- Nacos, Kafka, SkyWalking, Alertmanager, and MinIO are not part of the default demo stack.

## Product Scope

- Creating mixed-run execution workflows from scratch, trend analysis, and Self-APM are not part of the default v0.1 demo path; they remain roadmap and backlog surfaces.
- Advanced deterministic analysis and report-review panels are hidden by default.
- Webhook notifications are available but disabled by default.
- Built-in OIDC, LDAP, and SSO adapters are not included in v0.1 alpha.
- Enterprise approval, audit, and fine-grained governance flows are not part of the community alpha.

## k6 Runtime

- Dynamic k6 TPS control is not declared as a supported v0.1 alpha capability.
- Dynamic k6 TPS stays source-gated until the corresponding OpenLoadHub k6 source, license notice, tag, build command, and reproducible proof are public.
- The public repository must not depend on committed custom k6 binaries.

## Support Expectations

- Community support is best effort.
- Public issues should include clean reproduction steps and sanitized logs.
- Security reports must use the private reporting path in `SECURITY.md`.
