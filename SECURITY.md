# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive reports.

Email: **rossano.schifanella@isi.it** with:

- A description of the issue and its impact
- Steps to reproduce (or a proof-of-concept)
- Any relevant logs or configuration

You should receive an acknowledgement within **5 working days**. We aim to triage and respond with a remediation plan within **14 days**, and to ship a fix (or coordinated disclosure timeline) within **90 days** of the initial report.

## Supported versions

Until `v1.0.0`, only the latest `0.x` release is supported. Fixes will land on `main` and in a new patch release.

| Version | Supported |
|---|---|
| `0.x` (latest) | ✅ |
| older `0.x` | ❌ |

## Scope

This project bundles FastAPI, Martin, titiler, and other third-party components. Vulnerabilities in those upstreams should be reported to their respective maintainers; we'll track upgrades via Dependabot.
