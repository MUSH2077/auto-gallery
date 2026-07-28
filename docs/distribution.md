# Distribution and Privacy

auto-gallery is LAN-first software for personal media archives. Treat every
tracked file, commit, pull request, workflow log, artifact, and release archive
as public before changing repository visibility.

The official repository is:

```text
https://github.com/MUSH2077/auto-gallery
```

The official repository owner and URLs are public project metadata, not private
identifiers. Local usernames, hostnames, home paths, unrelated personal
remotes, private source accounts, and runtime media remain prohibited.

## Privacy checklist

Before every public push or release:

```bash
bash scripts/privacy-scan.sh
bash scripts/package-release.sh
```

Never commit or attach:

- `.env`, cookies, refresh tokens, API keys, passwords, SSH keys, or database
  dumps
- `data/`, `downloads/`, `library/`, app configuration, gallery-dl
  configuration, logs, backups, or service volumes
- private creator URLs, downloaded media, private account names, or browser
  debugging snapshots
- local paths such as `/home/<user>/...` or `/Users/<user>/...`

Use `.env.example` for placeholders and `docs/assets/` for fictional or fully
sanitized documentation images.

## History and visibility review

A clean working tree is not enough: Git history and GitHub Actions history can
still contain removed data. Before making a private repository public:

1. Search all Git objects for accidentally tracked environment files,
   credentials, dumps, keys, large media, and local account data.
2. Review Actions logs, artifacts, caches, release assets, and old pull requests.
3. Rotate any credential that may ever have been committed or printed. Deleting
   a file or rewriting history does not make an exposed secret safe.
4. Ask collaborators to remove stale clones after any required history rewrite.
5. Verify the repository while logged out after the visibility change.

History rewriting is disruptive and should only be performed after the exact
paths and coordination plan are reviewed.

## Release package

The release script starts from tracked files and removes collaboration context,
environment files, runtime paths, caches, and logs:

```bash
bash scripts/package-release.sh v0.1.0-beta.1
tar -tzf dist/auto-gallery-v0.1.0-beta.1.tar.gz | less
```

Docker Compose is the current distribution target. Hosted installers and cloud
deployment templates are outside the beta scope.
