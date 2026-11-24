# Architecture

Apphoryn has two product lanes and one shared automation plane:

1. **Installable app store** — only records with at least one verified installer appear in the public app catalogue.
2. **Source project workbench** — licensed repositories remain available for inspection and native builds even when they do not have a ready-to-install package.
3. **Scout and build automation** — discovers, imports, classifies, builds, validates, stores, deduplicates and refreshes both lanes.

```text
                 Public software sources
       GitHub repositories / Releases / seed sources
                         |
                         v
                Apphoryn Scout bot
          cursor + retry queue + stable repo ID
                         |
              metadata / README / license
                         |
                 SPDX policy gate
                         |
              +----------+-----------+
              |                      |
        Ready installer         Source project
              |                      |
       Download + hash       register source metadata
       format/sign check             |
              |                isolated native build
              |                (optional, per target)
              |                      |
              +----------+-----------+
                         |
                 verification engine
          format / size / digest / APK signature
                         |
                 Apphoryn storage
          content-addressed object store (API)
             + Apphoryn-owned GitHub Releases
                         |
                 immutable SHA-256 identity
                         |
                  static app manifest
                  /                 \
          project workbench      installable store
          all licensed sources   verified files only
                  |                 |
           build history       app detail/download
```

## Components

- `apps/web`: responsive multi-language storefront, source-project workbench, Scout controls, build lab, saved apps and download centre.
- `apps/android`: native Android wrapper that packages the same storefront assets and hands package downloads to Android DownloadManager.
- `apps/desktop`: Electron clients for Windows, Linux and macOS.
- `app/github_client.py`: GitHub REST API access and bounded retry handling.
- `app/repository_crawler.py`: resumable public-repository enumeration with independent discovery and refresh cursors.
- `scripts/harvest_and_import.py`: Scout worker, license gates, verified-release import, source-only registration and optional build fallback.
- `app/services.py`: discovery, license policy, release import, build dispatch and authenticated artifact ingest.
- `app/ai_descriptions.py`: optional OpenAI-compatible factual description writer. README excerpts are treated as untrusted data; model failures fall back to source metadata.
- `app/artifact_validation.py`: package-format checks and APK signature verification where Android build tools are available.
- `app/artifact_store.py`: atomic SHA-256 content-addressed local storage with duplicate-object reuse and path/symlink protections.
- `app/github_release_mirror.py`: Apphoryn-owned GitHub Release storage, static catalogue publication, source-project registration, build dispatch and idempotent build publication.
- `app/database.py`: SQLite catalogue, artifact and build-job records.
- `scripts/ci_build.py`: finite platform build adapters; unsupported projects fail explicitly.
- `scripts/publish_github_release.py`: independently verifies and publishes build outputs to Apphoryn-owned Releases.
- `.github/workflows`: unit/CI, global Scout, native client matrix and isolated per-project build pipelines.

## Record lifecycle

### Source project

A project is registered by stable GitHub repository ID and canonical URL after its licence and repository metadata pass policy. Its source metadata, commit/ref, build attempts and history remain available in the workbench. Registering a source project does **not** create a package Release or a fake downloadable app.

### Installable app

An app becomes installable only after a real package has been acquired or built and passed the configured verifier. Package SHA-256 is stored in the app record; repeated binaries do not create duplicate package entries. New verified binaries update the existing app record and preserve version history. Source-only records are excluded from `GET /api/v1/catalog/apps` and remain available through `GET /api/v1/catalog/projects`.

## Trust boundaries

1. GitHub repository metadata, README content, tags and release assets are untrusted input.
2. Third-party source build commands execute only on disposable native runners, without Apphoryn publishing credentials.
3. Publisher jobs download build outputs as data on a fresh runner; they do not execute third-party build code.
4. Admin and artifact-ingest credentials are separate.
5. The API computes SHA-256 locally. An upstream digest is marked verified only when supplied by the source and matched exactly.
6. APKs require `apksigner verify` when imported or published. A successful package-format check is not malware scanning or publisher identity verification.
7. Store downloads point to Apphoryn-owned storage assets or the Apphoryn API download route, not to the upstream repository page.
