# Security model and operator checklist

Apphoryn downloads and builds third-party code. It must be operated as a software supply-chain service, not as a trusted file mirror.

## Required before public launch

- Put the API behind HTTPS and a reverse proxy with request-size, timeout and rate limits.
- Use long, random, different values for \`APPHORYN_ADMIN_TOKEN\` and \`APPHORYN_INGEST_TOKEN\`.
- Keep tokens in environment variables or a secrets manager; never commit \`.env\`.
- Use GitHub-hosted ephemeral runners or isolated disposable self-hosted runners. Never build untrusted projects on the production Apphoryn server.
- Do not provide production cloud credentials, signing keys, administrator tokens or deployment secrets to third-party build commands. Apphoryn source builds execute on a separate matrix of disposable native runners; the publisher receives build outputs only as data on a fresh runner.
- Consider a human moderation/approval step before publishing newly discovered software.
- Add malware scanning, provenance/attestation verification, SBOM generation, vulnerability review and artifact retention policies before broad public distribution.
- Back up the database and artifact directory together; monitor storage capacity and cleanup policies.
- CORS is disabled by default (same-origin storefront/API). Configure \`APPHORYN_CORS_ORIGINS\` only with exact trusted HTTPS origins when the API is hosted separately.
- Use a supported license policy reviewed by counsel. The built-in SPDX allowlist is intentionally conservative and is not legal advice.

## Import rules

- Only HTTPS GitHub repository URLs in exact \`owner/repository\` form are accepted.
- Missing, unrecognized or non-allowlisted SPDX identifiers are blocked for automated mirroring.
- Archived repositories are not imported automatically.
- The importer fetches upstream license text and blocks automatic redistribution when the SPDX identifier is known but the corresponding license text cannot be retrieved for notice retention.
- Release downloads are restricted to GitHub release hosts and bounded by a configured size limit. Downloaded files are not shown in the storefront until format/hash/signature gates pass.
- SHA-256 values are always computed locally. Upstream verification is only marked true when the publisher-provided digest matches.
- APKs fail closed unless \`apksigner verify\` succeeds; the signing-certificate fingerprint is stored when reported. This does not independently establish the publisher's identity.
- Build jobs re-check license eligibility inside the runner. Ref names are validated before use.

## Artifact ingest

The optional self-hosted ingest endpoint requires a separate secret, checks source repository identity, recognized file extension, platform, size limit and supplied SHA-256, then writes the file beneath the configured artifact storage root. It also validates the package structure and rejects APKs when signature verification cannot run. Do not expose this endpoint without HTTPS and request-size enforcement.

## Reporting

Until a public security policy and contact address are configured by the project owner, report potential vulnerabilities privately to the repository maintainer. Do not publish secrets, access tokens or exploitable details in public issues.
