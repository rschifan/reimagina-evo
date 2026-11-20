# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive reports.

Email: **rossano.schifanella@isi.it** with:

- A description of the issue and its impact
- Steps to reproduce (or a proof-of-concept)
- Any relevant logs or configuration

You should receive an acknowledgement within **5 working days**. We aim to triage and respond with a remediation plan within **14 days**, and to ship a fix (or coordinated disclosure timeline) within **90 days** of the initial report.

## Supported versions

Report vulnerabilities against the current `main` source. Security fixes are developed on `main`. The source declares
version `1.0.0`, but its tag and release assets have not been published as of
20 November 2026. The latest published release is `v0.1.0`; use current source
for the installation procedure and verification results in this repository.

The prototype has no API authentication, and its RLS policies do not enforce
tenant isolation. Keep the API bound to loopback. See the
[API security scope](docs/architecture/API.md) and
[open architecture issues](docs/architecture/TODO.md) before planning a
production deployment.

## Scope

This project bundles FastAPI, Martin, titiler, and other third-party components. Vulnerabilities in those upstreams should be reported to their respective maintainers; we'll track upgrades via Dependabot.
