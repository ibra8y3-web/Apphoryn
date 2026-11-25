# Apphoryn autonomous operations

## What stays active

The Apphoryn web service starts its persistent agent workers during application startup. Workers remain available to consume queued work and recurring schedules; they do not continuously call language models while idle.

The workforce autopilot is enabled by default. Its persisted `agent_autopilot_enabled` setting can be changed from the admin workforce dashboard. A database setting takes precedence over the `APPHORYN_AGENT_AUTOPILOT_ENABLED` environment default, so the owner's choice survives normal redeployments.

## Shared catalog event flow

1. GitHub Actions runs Global Scout every two hours.
2. Global Scout incrementally crawls public GitHub repositories, filters archived/forked or license-ineligible projects, checks the latest upstream Release, downloads supported release assets, validates them, and publishes the canonical Apphoryn catalog and verified artifacts.
3. A bounded source-build dispatch is allowed for at most one repository per Scout run. Only targets detected by Apphoryn's existing build adapters are dispatched. Builds run on isolated GitHub-hosted runners without repository secrets; the separate trusted publisher validates output again before publishing.
4. The Railway agent worker polls the canonical public catalog at a bounded interval (five minutes by default). It records a durable catalog snapshot and queues one Niebex review only when application metadata, release versions, or artifact hashes actually change.
5. Niebex routes that review to relevant specialists and the Supervisor. These reviews are read-only: the automatic review run has no external-action or code-write approval. Global Scout remains responsible for the actual import/build/publish pipeline.

The catalog watcher does not fetch binaries or execute third-party code. The shared catalog is public and canonical; the storefront fetches it without browser caching and refreshes open sessions every two minutes and when a user returns to the app.

## Safety and operational boundaries

- A source project is not labelled installable until a real supported package has passed validation and is published.
- Unsupported source projects remain source-only; Apphoryn does not fabricate installers.
- Existing content-hash deduplication is preserved. Identical artifact bytes are not added repeatedly.
- Automatic source builds have a limit of one repository per two-hour Scout run. The platform adapters and build workflow impose their existing target and runtime bounds.
- Paid AI provider access remains disabled by default. The catalog watcher only queues a review on actual changes; it does not create a recurring model call while the catalog is unchanged.
- Manual code edits, direct writes to `main`, and unrestricted external actions are not enabled by workforce autopilot.
- The shared catalog is read by all storefront clients. Local saved/favourite state and whether a package is installed on a device remain per-user/per-device state.

## Configuration

Optional controls:

- `APPHORYN_AGENT_AUTOPILOT_ENABLED=true|false` — default for the persistent workforce catalog watcher. An admin dashboard choice stored in the agent database takes precedence.
- `APPHORYN_CATALOG_POLL_SECONDS` — catalog poll interval, clamped to 60–3600 seconds; default 300.
- `APPHORYN_CATALOG_REPOSITORY` — canonical `owner/repository`; default `ibra8y3-web/Apphoryn`.
- `APPHORYN_CATALOG_REF` — canonical catalog branch; default `main`.

Global Scout build targets default to `android,windows,linux,macos` with `APPHORYN_GLOBAL_SCOUT_MAX_BUILDS_PER_RUN=1`. Only targets supported by the trusted build-adapter detector are dispatched.

## Work/rest duty cycle

The catalog watcher can run on a fixed cycle instead of continuously. From the (password-gated) admin page set the work minutes and rest minutes; the work window starts when the cycle is saved. During rest the watcher is idle; manually submitted tasks and saved schedules still run. Rest `0` means continuous (the default).

- `APPHORYN_AGENT_WORK_MINUTES` / `APPHORYN_AGENT_REST_MINUTES` — defaults used until a cycle is saved from the admin page (default 60 / 0).
- `POST /api/v1/admin/agents/automation/schedule` — `{ "work_minutes": 60, "rest_minutes": 30 }`.

## Admin access

The admin page shows only a password screen until `APPHORYN_ADMIN_TOKEN` is verified (`GET /api/v1/admin/agents/verify`). The token is kept in memory only, the session locks after 15 idle minutes, and wrong passwords are never locked out (unlimited attempts; each wrong try is delayed 0.4 s). Set `APPHORYN_ADMIN_MAX_FAILED=N` to enable an optional per-visitor lockout.

### Running behind a reverse proxy

Set `APPHORYN_TRUSTED_PROXY_HOPS=1` (number of proxies in front of the app). Without it every visitor appears to come from the proxy address, so the failed-login lockout and the analytics rate limit would apply to all users together. Do not set it when the app is directly exposed, because `X-Forwarded-For` could then be spoofed.
