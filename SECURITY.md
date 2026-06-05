# Security Policy

auto-gallery is designed for self-hosted, LAN-first deployments. Do not expose
the admin web or backend API directly to the public internet without a reverse
proxy, TLS, access controls, and a threat model you understand.

## Supported Versions

The project is currently in beta. Security fixes are applied to the default
branch until tagged releases are introduced.

## Reporting a Vulnerability

Please report security issues privately to the maintainer before opening a
public issue. Include:

- Affected version or commit
- Deployment shape, without secrets
- Reproduction steps
- Impact and any known workaround

Do not include real cookies, refresh tokens, API keys, passwords, or private
creator URLs in public issues, screenshots, logs, or pull requests.

## Sensitive Data

auto-gallery can store gallery-dl cookies, site credentials, downloaded media,
metadata, and personal archive paths. Treat the following as secrets:

- `.env`
- gallery-dl cookies and config files
- database dumps and backups
- logs containing source URLs or authentication failures
- screenshots that show private creators, paths, or account names

## Scope

Security reports are in scope for the application code and Docker Compose
deployment files. Third-party site behavior, gallery-dl extractor breakage, and
platform account restrictions are handled as provider compatibility issues
unless they expose auto-gallery user data.
