# Apphoryn Global Scout and migration pipeline

## Discovery source

Scout uses GitHub REST API `GET /repositories?since=<id>&per_page=100`, not a Search API query loop. GitHub documents this endpoint as a listing of all public repositories in creation order. The `since` parameter returns repositories whose numeric repository ID is greater than the supplied cursor. The cursor is saved in `data/scout-state.json` and committed by the workflow after each run.

This removes the Search API's per-query 1,000-result cap. It also means the scanner sees public repositories regardless of topic, language, star count or project name. Private repositories are not enumerated.

## Batches and scheduling

The scheduled workflow runs every two hours. Default run limits are:

- Up to 200 repository records per run.
- Up to 25 public-listing pages per run.
- 100 repositories per fully consumed API page.
- The crawler saves the greatest processed repository ID and resumes from that ID on the next run.

The page is always consumed in full before the cursor advances. Therefore a batch can exceed its requested record limit by at most 99 records, but a remainder of a page is never silently skipped. If GitHub returns an empty page at the current end, the cursor is left unchanged so repositories created later are visible on the next run.

A full crawl is a long-running process, not a one-minute import. At the default cap, the theoretical upper bound is about 2,400 repository records per day before rate limits, filtering and run-time constraints. It can take many months to traverse a multi-million repository population. The workflow does not claim those repositories have already been crawled.

## Duplicate control

- The numeric GitHub repository ID is the primary immutable identity.
- Canonical HTTPS repository URL is a secondary identity.
- Database upserts and the static manifest merge by repository ID or canonical URL.
- A repository rename updates the existing record instead of making a second app.
- Public forks are scanned but skipped as separate store listings by default.
- Existing installable apps are rechecked in a round-robin queue for newer upstream release assets.
- Temporary network/import errors and release records with unsupported packages are stored in `data/scout-retry.json` with backoff.

## Migration and publication gate

The worker checks the current repository record, SPDX license eligibility, archived/disabled state, upstream release metadata, release asset hashes and package format. APKs must pass `apksigner verify`. Apphoryn mirrors only verified package files to Apphoryn-owned GitHub Releases.

A repository source tree, README, release page, or license notice is not an installable package. Projects with no supported verified installer remain outside the public installable catalogue. No notice-only release is counted as a store app.

## Native Apphoryn clients

- Android source project: `apps/android`; CI builds a debug-key signed APK for development.
- Desktop source project: `apps/desktop`; CI builds Windows NSIS, Linux AppImage/DEB and macOS DMG.
- Native-client build outputs are published only after every matrix target succeeds.
- Production Android signing, Windows code signing, and macOS Developer ID signing/notarization require owner-controlled credentials; unsigned/development packages are labelled accordingly.

## Reports

Every Scout run uploads `discovery-candidates.json`, `harvest-results.json` and `scout-report.json` as a workflow artifact. These are execution reports, not proof that every discovered source was installable.
