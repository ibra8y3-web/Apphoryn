# Apphoryn

**Open software. Built together.**

Apphoryn is an open-source software discovery, build and distribution platform. Its purpose is to discover real public software, verify redistribution eligibility, mirror actual upstream release packages or build them on isolated native runners, validate the resulting package formats, and store downloadable copies in **Apphoryn-owned GitHub Releases**.

> **Current code line: 0.2.0-alpha.** This is an actively developed platform. The storefront contains no fabricated app records or fake package files. The catalogue is populated by the first successful GitHub Actions Scout run, not by placeholder data.

## How the real store works

1. **Scout discovers projects.** It starts with the maintained seed list in \`catalog-seed-repositories.json\`, then searches GitHub for additional repositories.
2. **License gate.** Archived repositories, missing licenses and SPDX identifiers outside the built-in redistribution allowlist are rejected from automatic mirroring.
3. **Release importer.** It downloads actual release assets from the upstream project. It does not treat source code or a repository link as an installable package.
4. **Artifact verification.** SHA-256 is recalculated locally and compared with GitHub's release digest when supplied. The package is also inspected according to its real file format. APKs must contain an Android manifest and executable DEX/native code and must pass \`apksigner verify\`.
5. **Apphoryn mirror.** Validated binaries are uploaded to Releases in this Apphoryn repository. The generated \`apps/web/catalog/index.json\` stores their Apphoryn-owned download URLs and metadata.
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
- PWA shell, icons, dark visual identity, category cards, search, saved apps and download centre.
- Eight interface locales: English, Arabic (RTL), French, Spanish, German, Portuguese, Turkish and Indonesian. Some strings still fall back to English until translated.
- FastAPI backend and SQLite catalogue for self-hosted installations.
- GitHub repository discovery using the official REST API.
- SPDX license allowlist and archived-project rejection.
- Real upstream release-asset download and local SHA-256 calculation.
- Package format inspection for APK, EXE/PE, MSI/OLE, MSIX/APPX, AppImage/ELF, DEB/ar, RPM, DMG/UDIF, PKG/XAR, JAR and portable ELF/Mach-O outputs.
- APK signature verification via Android \`apksigner\`; signing certificate SHA-256 fingerprint is recorded when reported by the verifier.
- Apphoryn-owned GitHub Release storage for package binaries.
- Static catalogue manifest so the web storefront can operate without requiring a separate catalogue database service.
- Automatic Scout workflow on pushes to the default branches and every eight hours.
- Isolated build workflow for the supported Android Gradle, .NET, Go and Electron adapter patterns.
- Separate publisher job that treats build outputs as data and never executes them.
- Optional OpenAI-compatible AI descriptions based on repository metadata, topics and README excerpts. When AI credentials are absent, the importer uses upstream facts rather than inventing features.

## First run after upload

1. Upload the updated Apphoryn source to the GitHub repository's \`main\` branch (or \`master\` if that is the default branch).
2. Open **Settings → Actions → General → Workflow permissions** and allow the workflow token to have **Read and write permissions**. The workflow files also declare the minimum permissions they need: \`contents: write\` and \`actions: write\` for Scout; the publisher job receives \`contents: write\` only.
3. Open **Actions → Apphoryn Scout — import real software releases** and check the first run. A push to the default branch also starts the workflow automatically.
4. When the run succeeds, Apphoryn creates releases containing the actual binary files and commits the generated \`apps/web/catalog/index.json\` manifest. Open the storefront again; it reads the mirrored package list.
5. Open the repository's **Releases** page to inspect the actual stored package files and their SHA-256 data.

The first run needs network access to GitHub and Android SDK build-tools because APK imports are rejected unless \`apksigner\` can validate their signatures. If \`apksigner\` is unavailable, APKs are skipped rather than marked as verified.

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

- This code line is a responsive web/PWA storefront, not yet separate native Android, Windows, Linux and macOS client applications.
- A web page cannot silently bypass platform installation permissions. Android will show the operating system's package-install flow; Windows, macOS and Linux installers follow their own security and privilege prompts.
- Build adapters are deliberately limited. Android Gradle, selected .NET projects, Go modules and configured Electron projects are supported patterns, not universal compilers.
- A valid container/header and a matching SHA-256 do not prove that software is benign. APK signature verification confirms the package signature, not the publisher's identity or the absence of malware. EXE Authenticode, Windows package signatures, macOS notarization, SBOM/provenance attestations and malware scanning require additional dedicated verification stages before a large public launch.
- The built-in license allowlist is a technical policy gate, not legal advice. Each upstream license's notice, attribution, source-offer and redistribution obligations must be respected.
- GitHub Releases are the initial Apphoryn-owned binary storage backend. A large global store will eventually need object storage/CDN, database-backed catalogue indexing, quotas, moderation, and retention policies.
- No external GitHub workflow or real third-party binary import is claimed as executed from this editing session. The first real import happens when the updated workflow runs in the user's GitHub repository.

## License

The Apphoryn platform source in this repository is released under the MIT License. Imported applications and their binaries remain subject to their upstream licenses and notices.


## Autonomous store updates

The Apphoryn Scout GitHub Actions workflow runs hourly to search application-focused open-source categories, refresh release metadata, mirror verified installer files, and dispatch bounded isolated builds for supported applications without available packages. The public catalogue only lists applications after Apphoryn has a verified installable artifact. Native Android and desktop clients can install Apphoryn-owned release assets from within the application after SHA-256 verification.


Android clients install signed APKs and supported XAPK split bundles; XAPK files carrying OBB expansion data are currently rejected. Windows clients include EXE/MSI/MSIX/APPX install adapters, and macOS clients include PKG/DMG adapters.


Unsigned source-built Android release packages are never published as installable apps. To enable automatic publication of source-built APKs that are not already signed upstream, configure a persistent Apphoryn Android keystore in GitHub Actions secrets; those credentials are exposed only to the trusted publisher stage, never to untrusted source builds.
