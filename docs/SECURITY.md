# Security model and operator checklist

Apphoryn downloads and builds third-party code. It must be operated as a software supply-chain service, not as a trusted file mirror.

## Required before public launch

- Put the API behind HTTPS and a reverse proxy with request-size, timeout and rate limits.
- Use long, random, different values for `APPHORYN_ADMIN_TOKEN` and `APPHORYN_INGEST_TOKEN`.
- Keep tokens in environment variables or a secrets manager; never commit `.env`.
- Use GitHub-hosted ephemeral runners or isolated disposable self-hosted runners. Never build untrusted projects on the production Apphoryn server.
- Do not provide production cloud credentials, signing keys, administrator tokens or deployment secrets to third-party build commands.
- Consider a human moderation/approval step before publishing newly discovered software.
- Add malware scanning, provenance/attestation verification, SBOM generation, vulnerability review and artifact retention policies before broad public distribution.
- Back up the database and artifact directory together; monitor storage capacity and cleanup policies.
- Restrict CORS to your storefront domain before production; the starter enables permissive CORS for local development.
- Use a supported license policy reviewed by counsel. The built-in SPDX allowlist is intentionally conservative and is not legal advice.

## Import rules

- Only HTTPS GitHub repository URLs in exact `owner/repository` form are accepted.
- Missing, unrecognized or non-allowlisted SPDX identifiers are blocked for automated mirroring.
- Archived repositories are not imported automatically.
- Release downloads are restricted to GitHub release hosts and bounded by a configured size limit.
- SHA-256 values are always computed locally. Upstream verification is only marked true when the publisher-provided digest matches.

## Artifact ingest

The ingest endpoint requires a separate secret, checks source repository identity, recognized file extension, platform, size limit and supplied SHA-256, then writes the file beneath the configured artifact storage root. Do not expose this endpoint without HTTPS and request-size enforcement.

## Reporting

Until a public security policy and contact address are configured by the project owner, report potential vulnerabilities privately to the repository maintainer. Do not publish secrets, access tokens or exploitable details in public issues.
