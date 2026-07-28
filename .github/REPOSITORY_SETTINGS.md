# GitHub Repository Settings

This file records the intended public-repository configuration. Review it
after ownership, the default branch, or the maintainer team changes.

## About and social preview

- Description: `Self-hosted, LAN-first media archiving for creator-based collections.`
- Website: leave empty until a maintained documentation site exists.
- Topics: `self-hosted`, `gallery-dl`, `media-archive`, `nas`, `fastapi`,
  `nextjs`, `docker-compose`, `pixiv`, `twitter`
- Features: Issues enabled; Projects and Wiki disabled until actively used.
- Discussions: enable only when there is maintainer capacity to moderate them.
- Social preview: add a sanitized 1280 × 640 project image before launch.

## General

- Default branch: `master` (rename to `main` only as a separate, communicated
  migration).
- Merge methods: enable **Squash merging**; disable merge commits and rebase
  merging.
- Use the pull request title as the default squash commit message.
- Automatically delete head branches after merge.
- Enable Releases and preserve release notes in `CHANGELOG.md`.

## Rulesets

Keep the active repository rulesets synchronized with:

- `.github/rulesets/default-branch.json`
- `.github/rulesets/release-tags.json`

The rulesets were first activated after the repository became public. Forks
must import their own copies; versioned JSON is the portable source of truth.

The default-branch ruleset intentionally requires zero approvals while there
is only one maintainer. It still requires a pull request, resolved review
threads, a current branch, linear history, and the blocking build, integration,
dependency-review, and CodeQL checks.
Requiring one approval now would prevent the sole maintainer from merging their
own changes.

When a second active maintainer joins:

1. Set required approvals to `1`.
2. Enable stale-review dismissal.
3. Enable code-owner review.
4. Consider requiring approval of the most recent push.

Do not add broad organization or repository-admin bypasses. Use a narrowly
scoped emergency bypass only if there is a documented recovery procedure.

## Security and analysis

Enable:

- Dependency graph
- Dependabot alerts and security updates
- Grouped security updates where available
- Private vulnerability reporting
- Secret scanning
- Push protection
- CodeQL using the committed advanced workflow

If GitHub enables CodeQL default setup automatically, keep only one setup:
disable default setup before relying on `.github/workflows/codeql.yml`.

Public repositories run Dependency Review and CodeQL without a
`GHAS_ENABLED` variable. Set `GHAS_ENABLED=true` only for an eligible private
repository after GitHub Code Security is enabled.

## Actions

- Allow GitHub-owned actions and only the explicitly allow-listed third-party
  actions used by this repository.
- Require every action to be pinned to a full-length commit SHA.
- Require approval for workflows from first-time external contributors.
- Keep the default workflow token read-only.
- Do not expose repository secrets to workflows from forks.
- Review and remove old artifacts and logs containing sensitive data before
  changing repository visibility.

## Moderation and community

- Enable interaction limits during spam or abuse incidents.
- Keep blank issues disabled; route reports through the structured forms.
- Add maintainers to `CODEOWNERS` rather than granting unnecessary admin access.
- Review the community profile after the repository becomes public.

## Launch verification

- CI, CodeQL, and Scorecard have completed successfully on the default branch.
- The ruleset status checks exactly match the live check names.
- The Security tab exposes private vulnerability reporting.
- The About description, topics, license, and social preview render correctly.
- A logged-out browser can read the README, docs, issue forms, license, and
  latest release without exposing private Actions artifacts or historical logs.
