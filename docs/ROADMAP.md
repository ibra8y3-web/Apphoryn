# Product roadmap

## 0.1.0 — Store foundation (included)

- Responsive PWA storefront and eight locale options.
- GitHub candidate discovery and scheduled Scout workflow.
- SPDX allowlist and archived-repository checks.
- Latest-release artifact mirroring into Apphoryn storage.
- Optional metadata-grounded AI descriptions.
- Build dispatch, isolated runner matrix and separate artifact publishing job.
- SQLite catalog, SHA-256 tracking and direct artifact downloads.

## 0.2 — Installation and supply-chain hardening

- Resumable download queue with per-file progress, retry and pause/resume.
- Malware scanning and quarantine before catalog publication.
- SBOM generation, dependency vulnerability analysis and provenance attestations.
- Signed build attestations and stronger origin/digest verification.
- Android publisher-signing workflow with explicit owner-controlled key management.
- Windows code-signing and macOS signing/notarization integrations.
- Linux package integration with distro package managers where supported.
- Human moderation console and project takedown process.

## 0.3 — Native clients and ecosystem

- Android native client using platform package installation APIs.
- Desktop clients for Windows, Linux and macOS.
- Account sync, collections, update notifications and download history.
- Verified developer publisher accounts and signed metadata.
- Plugin/build-adapter SDK with conformance tests.
- More build adapters (Flutter, Qt, Rust, CMake packaging, Python applications and additional ecosystems).

A roadmap item is not an implemented feature until code and tests are present in the repository.
