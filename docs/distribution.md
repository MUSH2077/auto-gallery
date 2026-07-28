# Distribution and Privacy

auto-gallery is LAN-first software for personal media archives. Treat the
repository as public by default before publishing, opening issues, or building
release archives.

## Remote Repository

Use a neutral organization or project namespace for public distribution:

```bash
git remote set-url origin git@github.com:<neutral-org>/auto-gallery.git
git remote -v
```

Do not hard-code personal GitHub usernames, local hostnames, or local home
paths in tracked files. If a personal remote was used during development,
rewrite it only in your local Git config; do not document the personal URL.

## Privacy Checklist

Before pushing or publishing a release:

```bash
scripts/privacy-scan.sh
scripts/package-release.sh
```

Never commit or attach:

- `.env`, cookies, refresh tokens, API keys, passwords, SSH keys, or database
  dumps.
- `data/`, `downloads/`, `library/`, `app-config/`, `gallerydl-config/`, logs,
  backups, or Meilisearch/PostgreSQL/Redis volumes.
- Private creator URLs, downloaded media, screenshots with real creators, or
  Playwright debug snapshots.
- Local-only paths such as `/home/<user>/...`.

Use `.env.example` for placeholders only. Use sanitized SVG screenshots under
`docs/assets/` for public documentation.

## Release Package

The release package script uses tracked files and removes local collaboration
context and runtime paths:

```bash
scripts/package-release.sh v0.1.0
tar -tzf dist/auto-gallery-v0.1.0.tar.gz | less
```

The package intentionally excludes `.claude/`, `.playwright-mcp/`, `.env*`,
runtime data directories, caches, and logs. Docker Compose remains the v1
distribution target; Helm charts, hosted installers, and cloud deployment
templates are out of scope for now.
