# Apphoryn

**Open software. Built together.**

Apphoryn is an open-source software discovery, build and distribution platform. Its purpose is to discover real public software, verify redistribution eligibility, mirror actual upstream release packages or build them on isolated native runners, validate the resulting package formats, and store downloadable copies in **Apphoryn-owned GitHub Releases**.

> **Current code line: 0.3.0-alpha.** Apphoryn includes a PWA, a native Android client project, and an Electron desktop client project for Windows, Linux and macOS. Development install packages are produced by the native-client workflow; the store catalogue only exposes third-party apps after a real installer has passed import and format verification.

## Two linked product lanes: Store and Project Workbench

Apphoryn deliberately maintains two different kinds of records:

- **Installable store apps** have at least one real package file that has been downloaded or built, structurally validated, hashed, and copied into Apphoryn-owned artifact storage. The public storefront filters out records with no verified installer.
- **Source projects** retain their upstream repository, immutable GitHub repository ID, license metadata, source commit/ref, description and build history in the project workbench. A source project can be reviewed and built without appearing as a downloadable app.

The Project Workbench is exposed separately at `GET /api/v1/catalog/projects`; the app store remains at `GET /api/v1/catalog/apps`. This prevents a repository URL, source archive, library, or README from being misrepresented as an installable application.

When a source project has no verified package, the Scout may dispatch the isolated native build workflow when `APPHORYN_AUTO_BUILD_TARGETS` is enabled. This fallback also runs when an upstream Release exists but contains only source archives, checksums, documentation or unsupported file types. Successful build outputs are independently validated and published to Apphoryn's own release storage before the store manifest exposes them.

### Apphoryn-owned downloads and duplicate handling

Upstream sources are discovery and update inputs, not the store's download destination. The importer downloads an eligible package, validates its format and integrity, stores the binary in Apphoryn's content-addressed object storage, and mirrors the validated file into **this Apphoryn repository's Releases**. Store download entries resolve to those Apphoryn-owned assets (or the Apphoryn API download route); they do not send customers to the upstream project page.

Each upstream project is keyed by its stable GitHub repository ID as well as its canonical repository URL, so renames update the existing record. Each binary is keyed by SHA-256 within its app record, so the same file is not re-added merely because a publisher changes its filename or release tag. A genuinely new binary/version is appended to the existing app's release history instead of creating a duplicate app listing.

## How the real store works

1. **Scout enumerates public projects.** It starts with the maintained seed list in \`catalog-seed-repositories.json\`, then incrementally reads GitHub's public-repository listing endpoint using the last repository ID as a durable cursor. It does not depend on the Search API's 1,000-result window.
2. **License gate.** Archived repositories, missing licenses and SPDX identifiers outside the built-in redistribution allowlist are rejected from automatic mirroring.
3. **Release importer.** It downloads actual release assets from the upstream project. It does not treat source code or a repository link as an installable package.
4. **Artifact verification.** Each release file is downloaded using its real filename extension, SHA-256 is recalculated and compared with GitHub's release digest when supplied, and the package is inspected according to its real format. APKs must contain an Android manifest and executable DEX/native code and pass \`apksigner verify\`.
5. **Apphoryn mirror.** Only projects with at least one verified installer are added to the installable storefront. Validated binaries are uploaded to Releases in this Apphoryn repository. The generated \`apps/web/catalog/index.json\` stores Apphoryn-owned download URLs, immutable GitHub repository IDs and provenance metadata. Source-only projects and notice-only releases are not advertised as installable apps.
6. **Storefront.** The web/PWA reads the generated catalogue and downloads the file from Apphoryn's mirror. The user does not need to visit the upstream GitHub project to fetch the installer.
7. **Build fallback.** If a licensed project has no valid release package, Scout can optionally dispatch a separate platform-native build workflow. Third-party source code runs on disposable runners without Apphoryn publishing secrets. A second, fresh publisher runner validates and stores the resulting output.

The upstream project URL is retained for attribution and provenance. It is never used as the storefront's package-download URL when an Apphoryn mirror exists.

## Initial real-project seeds

The first Scout run checks these real upstream projects. It imports only supported release files that pass validation; a seed entry is not itself evidence that its latest release has already been mirrored.

| Project | Main use | Upstream license | Expected platforms |
|---|---|---|---|
| [LocalSend](https://github.com/localsend/localsend) | Nearby-device file sharing | Apache-2.0 | Android, Windows, Linux, macOS |
| [Shizuku](https://github.com/RikkaApps/Shizuku) | Android system API bridge | Apache-2.0 | Android |
| [KeePassDX](https://github.com/Kunzisoft/KeePassDX) | KeePass-compatible password manager | GPL-3.0 | Android |

The importer skips non-installable sidecars and the large Google Play-specific LocalSend asset by default. Only files that pass the format checks are added to the Apphoryn catalogue. The exact versions and package names are read from the upstream release at run time, not hard-coded in the UI.

## What is included

- Responsive storefront for phones, tablets, laptops and desktop screens.
- Native Android storefront project at \`apps/android\`; it packages the same storefront assets, serves them from a loopback origin, and uses Android DownloadManager for package downloads.
- Electron desktop storefront project at \`apps/desktop\`; it packages the same storefront assets and targets Windows NSIS installers, Linux AppImage/DEB and macOS DMG.
- CI build matrix for Android, Windows, Linux and macOS. Android CI output is debug-key signed for development; Windows/macOS packages remain unsigned unless proper signing certificates are configured.
- Installed clients can optionally point to a hosted Apphoryn API endpoint from Settings; an empty endpoint uses the packaged static catalogue.
- PWA shell, icons, dark visual identity, category cards, search, saved apps and download centre.
- Eight interface locales: English, Arabic (RTL), French, Spanish, German, Portuguese, Turkish and Indonesian. Some strings still fall back to English until translated.
- FastAPI backend and SQLite catalogue for self-hosted installations.
- Full public GitHub repository enumeration using the REST endpoint \`GET /repositories?since=<last_repository_id>\`; checkpointed incremental crawling, bounded batches and a retry queue.
- Stable repository-ID deduplication across upstream renames, canonical-URL deduplication, fork suppression and round-robin refresh of existing mirrored apps.
- Scheduled Scout runs every two hours; each run advances the saved public-repository ID cursor and records a machine-readable report.
- SPDX license allowlist and archived-project rejection.
- Real upstream release-asset download and local SHA-256 calculation.
- Package format inspection for APK, EXE/PE, MSI/OLE, MSIX/APPX, AppImage/ELF, DEB/ar, RPM, DMG/UDIF, PKG/XAR, JAR and portable ELF/Mach-O outputs.
- APK signature verification via Android \`apksigner\`; signing certificate SHA-256 fingerprint is recorded when reported by the verifier.
- Apphoryn-owned GitHub Release storage for package binaries.
- Static catalogue manifest so the web storefront can operate without requiring a separate catalogue database service.
- Automatic global Scout workflow on pushes to the default branches and every two hours.
- Isolated build workflow for the supported Android Gradle, .NET, Go and Electron adapter patterns.
- Separate publisher job that treats build outputs as data and never executes them.
- Optional OpenAI-compatible AI descriptions based on repository metadata, topics and README excerpts. When AI credentials are absent, the importer uses upstream facts rather than inventing features.

## First run after upload

1. Upload the updated Apphoryn source to the GitHub repository's \`main\` branch (or \`master\` if that is the default branch).
2. Open **Settings → Actions → General → Workflow permissions** and allow the workflow token to have **Read and write permissions**. The workflow files also declare the minimum permissions they need: \`contents: write\` and \`actions: write\` for Scout; the publisher job receives \`contents: write\` only.
3. Open **Actions → Apphoryn Scout — global repository crawl and migration** and check the run report. The scheduled worker advances through public repository IDs in order; it does not attempt to import millions in one workflow execution. Each run handles a bounded batch and saves the exact cursor for the next run.
4. Open **Actions → Build Apphoryn native store clients** to inspect the Android/Windows/Linux/macOS build matrix. A successful push to the default branch publishes a rolling development release tagged \`apphoryn-store-clients-dev\`.
5. When the Scout run successfully imports real installers, Apphoryn creates releases containing the binary files and updates \`apps/web/catalog/index.json\`. Source-only repositories and unsupported release files remain unlisted.
6. Open the repository's **Releases** page to inspect the actual stored package files and their SHA-256 data.

The first run needs network access to GitHub and Android SDK build-tools because APK imports are rejected unless \`apksigner\` can validate their signatures. If \`apksigner\` is unavailable, APKs are skipped rather than marked as verified.

### AI classification and safe build routing

The discovery worker records a project type before suggesting an automatic source build:

- `application` and `developer_tool`: eligible for automatic native-build fallback when `APPHORYN_AUTO_BUILD_TARGETS` is explicitly enabled and no verified installer exists.
- `library`, `framework`, `service`, `documentation`, and `unknown`: retained in the source workbench, but not automatically sent to installer build adapters.
- When `APPHORYN_AI_API_KEY` and `APPHORYN_AI_MODEL` are configured, the OpenAI-compatible description engine creates bounded factual descriptions and classifies the repository using its metadata and README excerpt. Without those credentials, a conservative evidence-based fallback is used.
- Previously heuristic source records are revisited when AI credentials become available so their classification can be upgraded without deleting or re-importing the project.

AI output is treated as metadata, not proof that a project can compile, that a binary is safe, or that redistribution is permitted. License checks, adapter detection, package validation, SHA-256 checks and Apphoryn-owned publication remain separate gates.

### Optional automatic source builds

In **Settings → Secrets and variables → Actions → Variables**, add:

- \`APPHORYN_AUTO_BUILD_TARGETS\` = a comma-separated list such as \`android,windows,linux,macos\`.

Leave it unset to mirror official release files without automatically compiling source projects. Enable it only after reviewing the security model and understanding that many GitHub projects do not use a supported build system. Unsupported projects fail visibly and are never published as successful builds.

Build targets use native GitHub-hosted runners. The build jobs do not receive the publishing token or signing keys. The publisher job gets a new runner and \`GITHUB_TOKEN\` with release/content write permission. Android release APKs must be cryptographically signed; the workflow does not silently substitute a debug APK.

### Optional AI-generated descriptions

Add repository Actions secret \`APPHORYN_AI_API_KEY\` and variable \`APPHORYN_AI_MODEL\`. The endpoint is configured with \`APPHORYN_AI_BASE_URL\` in the runtime environment. Do not add an AI key to build jobs or to any workflow that executes third-party source code. AI-generated descriptions are explanatory metadata, not proof of package safety.

## Run the self-hosted API and storefront

Requires Python 3.11+.

\`\`\`bash
cd Apphoryn
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
\`\`\`

Set a strong \`APPHORYN_ADMIN_TOKEN\` in \`.env\`, then start the server:

\`\`\`bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
\`\`\`

Open \`http://127.0.0.1:8000\`. The optional API stores local artifacts in \`storage/artifacts/\`; the GitHub Release mirror is the persistent shared distribution path used by the static storefront catalogue.

## Tests

\`\`\`bash
python -m compileall -q app scripts tests
python -m unittest discover -s tests -v
node --check apps/web/app.js
\`\`\`

GitHub Actions runs the local validation suite. Unit tests cover package-format rejection, catalogue merging and release-mirror metadata. These tests do not download or build the external seed apps; the scheduled Scout workflow performs that integration work after the repository is uploaded and its permissions are configured.

## Important limitations

- The native clients are real buildable source projects, but a build is not considered delivered until the native-client workflow succeeds and the resulting assets appear in the Apphoryn development release.
- The global scanner can enumerate public GitHub repositories as the saved cursor advances, but it does not migrate millions in a single run. GitHub rate limits, bounded Actions execution, license eligibility, available release files and verification all constrain the actual migration rate.
- A web page cannot silently bypass platform installation permissions. Android will show the operating system's package-install flow; Windows, macOS and Linux installers follow their own security and privilege prompts.
- Build adapters are deliberately limited. Android Gradle, selected .NET projects, Go modules and configured Electron projects are supported patterns, not universal compilers.
- A valid container/header and a matching SHA-256 do not prove that software is benign. APK signature verification confirms the package signature, not the publisher's identity or the absence of malware. EXE Authenticode, Windows package signatures, macOS notarization, SBOM/provenance attestations and malware scanning require additional dedicated verification stages before a large public launch.
- The built-in license allowlist is a technical policy gate, not legal advice. Each upstream license's notice, attribution, source-offer and redistribution obligations must be respected.
- GitHub Releases are the initial Apphoryn-owned binary storage backend. A very large catalogue will eventually need object storage/CDN, database-backed indexing, quotas, moderation, and retention policies.
- The repository code has tests for crawler checkpointing, stable repository identity, importer file-extension handling and desktop asset-path safety. Actual native package build success and real third-party asset mirroring must still be confirmed from the resulting GitHub Actions runs and Releases; source changes alone are not treated as a successful build.

## License

The Apphoryn platform source in this repository is released under the MIT License. Imported applications and their binaries remain subject to their upstream licenses and notices.
