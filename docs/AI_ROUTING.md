# AI provider routing

Apphoryn's AI router is server-side. The store client never receives provider credentials. The existing Scout job invokes it through the factual app-description generator; the administrative API can display provider/model availability without returning secrets.

## Repository Actions secrets

Add only actual credentials under **Settings → Secrets and variables → Actions → Secrets**:

- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `OPENROUTER_API_KEY`
- `COMET_API_KEY`
- `CLOUDFLARE_API_TOKEN`

The Scout workflow passes these values only to its trusted discovery job. They are not passed to third-party source-build jobs. `CLOUDFLARE_ACCOUNT_ID` is a non-secret Actions variable, not a token.

`CENTRAL_PORTAL_USERNAME` is an account username, not an AI API key, and is deliberately not read by the router.

## Free-only controls

- `APPHORYN_AI_FREE_ONLY=true` is the default.
- OpenRouter discovery accepts explicit `:free` model IDs and the `openrouter/free` router only.
- Gemini and Groq model-list endpoints do not expose an enforceable account billing tier. In free-only mode these providers remain paused until the owner sets the repository variable `APPHORYN_AI_CONFIRM_GEMINI_GROQ_FREE_TIER=true` after checking the provider consoles. This is an owner attestation, not a technical guarantee; keep provider billing caps/alerts at zero or use non-billable free-tier projects.
- CometAPI is queried through `/api/models`. In free-only mode, the router admits only models whose returned catalog metadata explicitly reports zero prompt and completion prices (or an explicit `free` flag). Trial/credit-priced models remain excluded unless credit-based use is explicitly enabled with `APPHORYN_AI_ALLOW_CREDIT_BASED_PROVIDERS=true` and a model allowlist.
- Cloudflare requires `CLOUDFLARE_ACCOUNT_ID` and remains disabled unless `APPHORYN_AI_ENABLE_CLOUDFLARE=true`. Cloudflare account usage and plan limits must be checked independently.

Do not set `APPHORYN_AI_FREE_ONLY=false` unless paid model use is explicitly authorized.

## API endpoints

Both endpoints require the existing `X-Apphoryn-Admin` header:

- `GET /api/v1/admin/ai/models`: cached provider/model inventory and availability.
- `POST /api/v1/admin/ai/models/refresh`: force a fresh provider model-list request.

The response contains provider names, model IDs, context metadata and status notes. It never contains API keys.

## Rotation behavior and limits

The router tries available models in configured provider priority order and fails over to another candidate after provider errors such as rate limits or transient server errors. Model discovery is cached for `APPHORYN_AI_MODELS_CACHE_SECONDS` (default 900 seconds). Provider APIs do not share a standard quota endpoint, and some do not report remaining free quota at all; therefore the router cannot promise a pre-exhaustion switch for every provider. HTTP 429 responses trigger model cooldown and fallback, and quota resets are discovered on later requests/model refreshes.

The current integration uses AI during catalog metadata generation. It is not a general-purpose autonomous agent or a continuously running 24/7 worker by itself. The existing Scout workflow runs on its configured schedule; a continuously running hosted worker and durable quota telemetry are separate deployment work.

The legacy single-provider OpenAI-compatible fallback is disabled by default. Set `APPHORYN_AI_ALLOW_LEGACY_PROVIDER=true` only when you explicitly authorize that endpoint and its billing; it is not used as an automatic paid fallback.


## Automated catalog worker

The trusted Scout workflow runs hourly and also on relevant pushes/manual dispatch. It searches broad application topic families without a minimum star count, refreshes source metadata, and records only verified installable artifacts in the public catalog. Source-only projects remain internal until an isolated build produces a verified package.

Automatic builds are limited to AI/source-evidence-classified standalone applications and executable developer tools. Existing installable platforms are skipped; only missing targets are dispatched. A per-run build budget prevents unbounded workflow fan-out. Third-party source code runs only on disposable native build runners with no Apphoryn provider or ingest secrets.

Descriptions are generated in English and Arabic. When the primary model omits Arabic text, the same provider router is called for a dedicated Arabic translation. If no eligible model is available, the record is marked `english_only` and AI is not represented as having succeeded.

The store UI fetches the current public catalogue JSON from Apphoryn's GitHub raw mirror on each launch when the optional FastAPI service is not configured. The bundled catalogue is retained as an offline fallback. Native installers fetch only assets in Apphoryn-owned GitHub Releases, follow allowlisted GitHub asset redirects, verify SHA-256, and then hand the file to the operating-system installer.


## Project-aware source-build dispatch

Before a build is dispatched, Scout reads the repository tree and package manifests without executing project code. It only selects targets supported by an existing Apphoryn adapter: Android requires a Gradle wrapper; desktop targets require a .NET project, Go module, or Electron package with a packaging script. Projects with no matching adapter remain source-only/internal and are not published as installable applications. The disposable build workflow still performs the actual build and validation before publishing.
