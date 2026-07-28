# Security Policy

auto-gallery is designed for trusted, self-hosted, LAN-first deployments. Do
not expose the admin web or backend API directly to the public internet without
a reverse proxy, TLS, access controls, network restrictions, and a threat model
you understand.

## Supported versions

Until the first stable release, security fixes are applied to the default
branch and the newest published beta only. Older commits and unmaintained
provider configurations are not supported.

| Version | Supported |
|---|---|
| Default branch | Yes |
| Latest tagged beta | Yes |
| Older tags and commits | No |

## Report a vulnerability

Do not open a public issue.

Use GitHub's private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Include the affected version or commit, deployment shape without secrets,
reproduction steps, impact, and any known workaround. A minimal sanitized
proof of concept is useful; real credentials and private media are not.

If private vulnerability reporting is temporarily unavailable, open a public
issue that contains no vulnerability details and asks a maintainer to provide a
private channel.

The project has no guaranteed response SLA. Maintainers will acknowledge a
valid report as capacity allows, coordinate remediation privately, and credit
reporters unless anonymity is requested.

## Sensitive data

auto-gallery can store gallery-dl cookies, platform credentials, source URLs,
downloaded media, metadata, and local archive paths. Treat these as secrets:

- `.env` and service credentials
- gallery-dl cookies and configuration files
- database dumps, backups, and search indexes
- logs containing source URLs or authentication failures
- screenshots containing private creators, paths, or account names

Never include them in public issues, discussions, pull requests, CI artifacts,
or reproduction repositories.

## Security scope

In scope:

- Authentication, authorization, and session handling
- Secret exposure through APIs, logs, exports, or UI
- Path traversal, command injection, SSRF, and unsafe file processing
- Cross-user access or unintended media disclosure
- Docker Compose defaults and dependency vulnerabilities with a practical
  impact on auto-gallery

Usually handled as compatibility or support issues:

- Third-party source downtime or gallery-dl extractor breakage
- Platform account restrictions and rate limiting
- Deployments that intentionally disable documented access controls
- Vulnerabilities that only exist in unsupported, modified deployments

The final classification depends on demonstrated impact to auto-gallery users.
