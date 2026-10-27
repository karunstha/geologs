# ADR-0001: Approved app image updates

## Status
Accepted

## Context
GeoLogs runs as a source-built Compose app with an owner-managed PostgreSQL database. Startup may migrate or rebuild stored data. Unattended updates need a recovery point and a clear distinction between compatible app changes and upgrades needing human preparation.

## Decision
Publish signed multi-platform images for tagged releases. Promote compatible releases through an approval-gated `stable-auto` channel. A host-side timer resolves and verifies the image digest, validates a database dump, then recreates only the app. It checks database-backed health and an authenticated API request, and restores the previous image on failure. Promotion refuses changes to the known schema and deployment files since the previous approved release. Operators handle database and configuration changes manually.

## Consequences

### Positive
- Installations update at a chosen time without giving a container Docker socket access.
- Each installation records the exact image digest and has a fresh database backup.
- The database container does not move during an app update.

### Negative
- The host needs Docker, Cosign, disk space for backups, and a scheduler.
- A failed release with incompatible data writes can require a manual database restore.
- The checkout's Compose and updater files still need manual maintenance.

## Alternatives considered
- Scheduled `git pull` and rebuild: no release gate or immutable artifact.
- A Docker socket mounted in an updater container: broad control over the host without improving migration handling.
- Auto-install every GitHub release: release publication alone does not establish database compatibility.

## References
- [Deploying](../deploying.md)
- [Releasing](../releasing.md)
- [Upgrading](../upgrading.md)
