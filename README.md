# Apphoryn

**Open software. Built together.**

Apphoryn is an open-source software discovery, build and distribution platform. Its goal is to discover genuine public software projects, check redistribution eligibility, build actual installable packages with isolated CI runners, verify artifact hashes, and store those files in an Apphoryn-owned artifact library.

> **Current release: 0.1.0 — starter platform, not a production app store.** The catalog intentionally starts empty. No demo apps, fabricated downloads, fake ratings, or simulated install results are seeded.

## What is included

- Responsive storefront UI for phone, tablet, laptop and desktop layouts.
- PWA manifest and offline shell cache.
- Eight interface locales: English, Arabic (RTL), French, Spanish, German, Portuguese, Turkish and Indonesian. English is the fallback for untranslated strings in this first release. Optional AI descriptions can also provide per-app summaries in these locales.
- FastAPI backend and SQLite catalog.
- GitHub repository discovery through the official REST API.
- Conservative SPDX license allowlist before automatic mirroring.
- Real latest-release asset importer for recognized package formats (APK/AAB, EXE/MSI/MSIX, AppImage/DEB/RPM, DMG/PKG and selected portable formats).
- Local artifact storage and direct download endpoints served by Apphoryn.
- SHA-256 calculation and upstream digest comparison when GitHub supplies a digest.
- Optional OpenAI-compatible factual description generation. Without configured AI credentials, the importer uses repository metadata instead of inventing content.
- GitHub Actions workflows for tests, scheduled GitHub discovery, license-gated automatic import (when configured), and platform-native build requests.
- Initial build adapters for Android Gradle, .NET publishing, Go modules and Electron packaging. Unsupported projects fail visibly rather than being marked as built.

## Quick start

Requires Python 3.11+.

```bash
cd Apphoryn
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at least a strong `APPHORYN_ADMIN_TOKEN`. Then run:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`.

The read-only storefront and GitHub discovery do not require the admin token. Import/build actions are protected. The token is entered into the Build Lab or supplied to API requests; the browser UI does not persist it in local storage.

## Import a real GitHub release

1. Open **AI discovery** and search for a real repository, for example `topic:opensource stars:>100`.
2. Only candidates with an SPDX license on the current allowlist and without an archived status can be imported automatically.
3. Enter the admin token in **Build Lab**.
4. Return to discovery and choose **Import**.
5. Apphoryn stores the project metadata and downloads recognized assets from its latest GitHub release into `storage/artifacts/`.
6. The catalog shows actual stored artifacts and Apphoryn serves them from `/api/v1/downloads/{artifact_id}`.

A source-only repository can still be sent to the build workflow, but it is not shown as having an installable package until a real artifact is published.

## Enable GitHub Actions builds

Push this folder to a GitHub repository first. In the repository's **Settings → Secrets and variables → Actions**, add:

- `APPHORYN_INGEST_URL`: public HTTPS base URL of the deployed Apphoryn instance, without a trailing slash.
- `APPHORYN_INGEST_TOKEN`: same secret configured as `APPHORYN_INGEST_TOKEN` in the Apphoryn server environment.

In Apphoryn's `.env`, configure:

```dotenv
APPHORYN_GITHUB_TOKEN=github_pat_with_repo_actions_write_and_contents_read
APPHORYN_GITHUB_OWNER=your-owner
APPHORYN_GITHUB_REPOSITORY=Apphoryn
APPHORYN_GITHUB_REF=main
APPHORYN_BUILD_WORKFLOW=build-app.yml
APPHORYN_ADMIN_TOKEN=long-random-admin-secret
APPHORYN_INGEST_TOKEN=different-long-random-ingest-secret
APPHORYN_PUBLIC_BASE_URL=https://your-store.example
```

The GitHub token needs permission to read the Apphoryn repository contents and dispatch its Actions workflow. Do not commit `.env` or expose tokens in screenshots, issues or logs.

For scheduled Scout imports, also add repository Actions secrets `APPHORYN_API_BASE_URL` (the public HTTPS Apphoryn server URL) and `APPHORYN_ADMIN_TOKEN` (matching the server admin token). Scout runs every eight hours, discovers up to five candidates, and imports only non-archived projects whose SPDX license is on the allowlist. It saves a report as a workflow artifact. To opt into auto-building imported projects that have no release packages, add the repository Actions variable `APPHORYN_AUTO_BUILD_TARGETS` with a comma-separated value such as `linux` or `android,windows,linux,macos`. Leave it empty to discover/import releases without dispatching builds.

The build workflow runs on disposable GitHub-hosted runners. Compilation and publication run in separate jobs on separate runners: the source build job never receives the Apphoryn ingest secret, and the publisher downloads the completed output as data on a fresh runner. Artifacts are also retained as GitHub Actions workflow artifacts. The server-ingest step is skipped when the two ingestion secrets are not configured.

## Optional AI descriptions

Apphoryn can call an OpenAI-compatible chat-completions endpoint to write a concise factual app summary and description from repository metadata, topics and a README excerpt. Configure:

```dotenv
APPHORYN_AI_BASE_URL=https://api.openai.com/v1
APPHORYN_AI_API_KEY=your-key
APPHORYN_AI_MODEL=your-enabled-model
```

No AI key is bundled. The importer falls back to repository-provided facts if AI is unavailable. AI output is bounded and should be reviewed before public production publishing.

## Docker

```bash
cp .env.example .env
# Set secrets and GitHub configuration in .env
docker compose up --build -d
```

Persistent SQLite data and built/imported artifacts live under `storage/`.

## Tests

```bash
python -m compileall -q app scripts tests
python -m unittest discover -s tests -v
node --check apps/web/app.js
```

GitHub Actions runs the same checks on pushes and pull requests.

## Important limitations in 0.1.0

- This release is a responsive web/PWA storefront, not yet separate native Android, Windows, Linux and macOS client applications.
- Installing an APK/EXE/DMG still follows the operating system's own permission, signing and installer rules. A web page cannot silently bypass them.
- Build adapters are deliberately limited and do not compile every repository. Android Gradle, some .NET projects, Go modules and correctly configured Electron projects are the current adapters. The project must have the relevant SDK/toolchain and provide a recognized installable output.
- A successful compilation is not a security certification. Third-party source code is untrusted; production operators should use isolated runners, review projects, enforce resource limits and use malware scanning before publication.
- No GitHub project is mirrored just because it is public. Missing, custom, unclear or disallowed licenses are blocked from automated redistribution pending human review.
- The PWA shell can be cached offline, but catalog search, downloads, discovery and builds require a running server and network access.

## License

The Apphoryn platform source in this repository is released under the MIT License. Imported third-party applications remain subject to their own licenses and attribution/notice requirements.
