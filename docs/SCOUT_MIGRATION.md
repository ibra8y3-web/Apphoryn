# Apphoryn global Scout and migration

## Discovery cursor

The global Scout uses GitHub's public repository listing endpoint, `GET /repositories?since=<repository-id>&per_page=100`. GitHub documents that this endpoint lists public repositories in creation order and that `since` returns IDs greater than the supplied repository ID. The checkpoint stores the last fully consumed ID so a run can resume after interruption.

A page is consumed completely before the cursor advances. This avoids losing the unprocessed tail of a page when a run reaches its configured repository budget. The reported count can therefore exceed the requested budget by at most one page.

## Refresh pass

A separate refresh cursor cycles through older public repositories after the forward scan reaches the current end. This gives previously scanned projects a chance to publish new releases without resetting the forward-discovery cursor.

The refresh cursor and forward cursor are persisted independently in `data/scout-state.json`. They are operational checkpoints, not a claim that the entire GitHub corpus has already been scanned.

## Import and publication safeguards

- Forks, archived repositories and disabled repositories are skipped.
- Import is blocked unless the upstream license is on Apphoryn's redistribution allowlist and its license text can be retrieved.
- Only packages that pass Apphoryn's format validation are published as installable artifacts.
- Projects without a verified installer remain source-only metadata and are not shown as installable storefront entries.
- Third-party source builds continue to run in isolated native build workflows, never in the trusted Scout process.
- Failed transient operations are recorded in `data/scout-retry.json` with bounded exponential retry intervals.

## Storage and validation

Newly imported and uploaded artifacts are copied into atomic SHA-256 content-addressed storage under `storage/artifacts/objects/<prefix>/<sha256><extension>`. Existing artifact database rows and their historical storage paths remain readable; no migration or deletion of existing files is performed.

Windows executables must contain a runnable PE entry point in an executable section. Linux ELF files must contain a valid executable load segment covering their entry point. JAR files must contain compiled classes and a manifest with a `Main-Class` entry; Java libraries without an application entry point are rejected.

## Operating limits

Each run is bounded by `--max-repositories` and `--max-pages`. A complete GitHub page is always consumed, so the number processed can exceed the repository limit by less than 100. Increase limits gradually and monitor GitHub API rate limits and Actions usage.
