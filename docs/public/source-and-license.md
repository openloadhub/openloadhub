# Source And License Notes

This document records the public alpha source disclosure policy.

## Platform

The OpenLoadHub platform repository should contain source code, public docs, demo configuration, and reproducible build instructions.

The platform repository uses Apache-2.0 for OpenLoadHub platform source code, public documentation, demo Compose files, and Docker build wiring.

It should not contain:

- custom k6 binaries
- full Apache JMeter binary distributions
- local runtime artifacts
- private process notes
- private deployment files

## k6 Runtime

k6 is licensed under AGPL-3.0. The OpenLoadHub platform repository does not relicense k6, custom k6 forks, or xk6-derived binaries under Apache-2.0.

OpenLoadHub v0.1 alpha uses standard public k6 release binaries in the demo image and does not claim dynamic rate control support. Dynamic rate control is planned for v0.2 after the corresponding OpenLoadHub k6 source fork, tag, build command, AGPL-3.0 license notice, and runtime proof are public.

Until that gate passes:

- do not commit a custom k6 binary
- do not claim dynamic rate control as an enabled alpha feature
- document it as a planned v0.2 capability

## JMeter Runtime

JMeter should be downloaded from official Apache distribution sources during image build.

Third-party JMeter plugins may be included only with a manifest that records:

- name
- version
- source URL
- checksum
- license
- target directory
- proof command

The public repository should keep only platform overlay files and build wiring.

The first public manifest is maintained in `docs/public/jmeter-third-party-manifest.md`.

## Public Runtime Images

The public export writes sanitized Dockerfiles to the standard runtime paths used by `docker-compose.demo.yml`.

The public agent Dockerfile:

- downloads official k6 release binaries and verifies SHA256
- downloads Apache JMeter and verifies SHA512
- downloads the gRPC JMeter plugin and verifies SHA256
- disables k6 scenario hot patching by default because the custom k6 source disclosure gate is still pending

The public admin, worker, and frontend Dockerfiles must use publicly reachable base images and build sources. They must not depend on private registries or non-public internal base images. The current public-safe Dockerfiles may still reference public mirror endpoints in the repo; treat those as implementation detail, not as a promise that every runtime image is already pinned to `docker.io` official sources.
