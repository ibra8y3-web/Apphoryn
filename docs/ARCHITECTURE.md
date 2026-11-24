# Architecture

```text
GitHub REST API
   │
   ├── Discovery ──> candidate list (no auto-publish)
   │                    │ curator/admin import
   ├── Repository metadata + README + latest release
   │                    │ SPDX allowlist gate
   │                    ▼
   │              Catalog Service ──> SQLite
   │                    │
   │                    └── release asset mirror ──> storage/artifacts
   │
   └── Actions workflow dispatch
          │
          ▼
   ephemeral platform runner
          │ source checkout (no Apphoryn ingest secret)
          ▼
   adapter build ──> artifact hash ──> Actions artifact retention
                                      │
                                      ▼ post-build only
                              Apphoryn ingest endpoint
                                      │
                                      ▼
                           storage/artifacts + SQLite
                                      │
                                      ▼
                              direct store download
```

## Components

- `apps/web`: responsive multi-language PWA storefront.
- `app/github_client.py`: GitHub REST API access.
- `app/services.py`: discovery, license gate, release mirroring, build dispatch and artifact ingestion.
- `app/ai_descriptions.py`: optional OpenAI-compatible factual catalog descriptions with metadata fallback.
- `app/database.py`: SQLite catalog, artifact and build-job records.
- `scripts/ci_build.py`: finite platform adapter runner; unsupported projects fail explicitly.
- `scripts/publish_artifacts.py`: sends build outputs to the configured HTTPS ingest endpoint after compilation.
- `.github/workflows`: CI, candidate harvesting and native-runner build matrix.

## Trust boundaries

1. GitHub repository metadata is untrusted input.
2. Third-party source build commands are untrusted code.
3. Admin and artifact-ingest credentials are separate.
4. Ingest credentials are not passed to the source build step.
5. Every stored artifact has a server-computed SHA-256 digest.
6. A matching upstream digest is distinguished from a locally computed hash.
7. The public catalog is served from persisted records; there is no seeded mock inventory.
