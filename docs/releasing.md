# Releasing GeoLogs

1. Bump `project.version` in `pyproject.toml` and `__version__` in `src/geologs/__init__.py`, then merge the change. Use `vMAJOR.MINOR.PATCH` tags.
2. Publish a GitHub release for that tag. `.github/workflows/release.yml` checks the version, runs the database and browser module tests, builds amd64/arm64 images, publishes build provenance and an SBOM, and signs the digest. Failed tests publish no image.
3. Read the release notes and choose whether unattended installation is safe. If so, run the **Promote auto-update release** workflow with its tag. Protect its `auto-update` GitHub environment with a required reviewer. The workflow verifies the image signature and provenance, compares it with the previous approved tag, and rejects changes to the database upgrade code, Compose file or environment template. A first promotion establishes the baseline.
4. Releases that require a new setting, schema or data migration, database image change, or unusual operator work stay outside the auto channel. Document a manual path in [Upgrading](upgrading.md).

The moving `stable-auto` tag is only a discovery pointer. Hosts verify its signature and install its immutable digest. Operators can pause a host without losing its pin. A later compatible release may be promoted after a manual upgrade establishes a new baseline.

The app reads the update status file under `update-status/`; it contains no credentials. Backup files and `.env` copies under `backups/` are private and excluded from Git. Keep copies outside the Docker host.
