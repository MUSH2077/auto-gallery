# Project Governance

auto-gallery is currently maintained by a small, maintainer-led community. This
document makes decision and release authority explicit without creating process
that the project cannot yet sustain.

## Roles

- **Contributors** open issues, propose changes, review work, and submit pull
  requests.
- **Maintainers** triage reports, review and merge changes, manage releases and
  security reports, and enforce the Code of Conduct.

The current review owners are listed in [.github/CODEOWNERS](.github/CODEOWNERS).
Adding a code owner does not automatically grant repository administration.

## Decisions

Routine decisions are made through focused issues and pull requests. Maintainers
prefer evidence from tests, migration safety, operational impact, accessible
interaction, and source-platform compatibility over voting by reaction count.

Open a design issue before changes that:

- break an API, database, storage, or provider contract;
- add a new long-running service or external infrastructure dependency;
- alter authentication, authorization, quarantine, deletion, or restore
  behavior;
- materially expand the project's legal or security exposure.

Maintainers seek rough consensus, but the active maintainer has final
responsibility when consensus cannot be reached. The decision and its tradeoffs
should be recorded publicly unless it contains embargoed security information.

## Merge and release authority

Changes reach the default branch through protected pull requests and required
CI. The active rules are documented in
[.github/REPOSITORY_SETTINGS.md](.github/REPOSITORY_SETTINGS.md).

Maintainers may merge a change after required checks pass and review
conversations are resolved. Security fixes may use a private fork and embargoed
advisory; normal checks still run before the public release where practical.

Only maintainers create release tags and GitHub releases. Release tags are
immutable. Releases follow [docs/release-checklist.md](docs/release-checklist.md)
and document migrations, upgrade steps, compatibility changes, and rollback.

## Becoming a maintainer

There is no automatic promotion threshold. A contributor may be invited after
a sustained record of technically sound, respectful work; reliable review;
attention to privacy and data safety; and familiarity with the project
architecture. Access starts at the minimum permission needed and can be removed
after prolonged inactivity or for security and conduct reasons.

When at least two active maintainers exist, the default-branch ruleset should
require one approval and code-owner review, as described in the repository
settings checklist.

## Conflicts and conduct

Maintainers disclose material conflicts of interest and recuse themselves from
conduct reports in which they are directly involved. Community conduct is
governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md); vulnerabilities use the
private process in [SECURITY.md](SECURITY.md).

This governance model may change as the maintainer group grows. Material
changes use the same public review process as other repository policy.
