# Security Policy

## Supported versions

Work Harness is currently alpha software. Security fixes are applied to the latest minor
release and the `main` branch.

| Version | Supported |
| --- | --- |
| 0.2.x | Yes |
| < 0.2 | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private vulnerability
reporting form under **Security → Advisories → Report a vulnerability** for this repository.
Include the affected version, impact, reproduction steps, and any suggested mitigation. Do
not include live credentials or unrelated personal data.

Maintainers will acknowledge a complete report as soon as practical, investigate it privately,
and coordinate disclosure after a fix or mitigation is available. Reports that only describe
unsupported configurations, exposed user credentials, or upstream model behavior may be
redirected, but they are still welcome when the Work Harness boundary is unclear.

## Deployment boundary

- The built-in API Key is a shared deployment secret, not user identity or tenant isolation.
- SQLite deployments are single-instance only.
- Distributed API/Worker deployments require the PostgreSQL profile; never share SQLite files
  between replicas or publish the PostgreSQL port.
- Provider secrets may come from environment variables or the authenticated settings API in
  embedded mode. UI-provided keys are encrypted at rest, never returned by read APIs, and must not
  be placed in YAML, frontend variables, logs, or issue reports.
- Distributed mode freezes Provider, Prompt, and runtime settings; all instances must receive the
  same deployment configuration. Its dead-letter endpoints currently use the shared deployment
  key and are not a separate administrator role.
- The bundled PostgreSQL Compose stack uses one database initialization/runtime role. Higher-trust
  deployments should use an external TLS database with separate migration and DML roles.
- Public deployments require HTTPS, ingress rate limits, backups, and an external identity
  layer when more than one trusted operator has access.
- The unauthenticated `/health/live` endpoint exposes only process liveness.
