# Apphoryn Global Scout and migration pipeline

## Discovery source

Scout uses GitHub REST API `GET /repositories?since=<id>&per_page=100`, not a Search API query loop. GitHub documents this endpoint as a listing of all public repositories in creation order. The `since` parameter returns repositories whose numeric repository ID is greater than the supplied cursor. The cursor is saved in `data/scout-state.json` and committed by the workflow after each run.

This removes the Search API's per-query 1,000-result cap. It also means the scanner sees public repositories regardless of topic, language, star count or project name. Private repositories are not enumerated.

## Batches and scheduling

The scheduled workflow runs every two hours. The forward discovery cursor processes up to 200 repositories per run (the default) and at most 25 pages. Every API page is consumed in full (100 records maximum) before its cursor advances; a batch can exceed its requested record limit by at most 99 records. The next run resumes from the saved immutable repository ID.

When the forward cursor reaches GitHub's current tail, Scout starts a second independent cursor from the beginning. This refresh pass walks older repositories again and looks for new releases, so a project scanned before its first installer appears can become installable later. The refresh cursor advances independently from the forward discovery cursor and is reset after a complete pass. Newly created repositories continue to be picked up by the forward cursor.

When the public listing returns an empty page, the corresponding cursor remains unchanged. This means repositories created later are visible in the next run; it also avoids falsely advancing beyond the current tail.

A full crawl is a long-running process, not a one-minute import. At the default 200-record cap and a two-hour schedule, the theoretical upper bound is about 2,400 repository records per day before rate limits, filtering and run-time constraints. The manual workflow can raise the per-run limit, subject to its execution window and GitHub API quotas. The total public-repository population is not claimed to have been scanned or migrated in advance.

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
