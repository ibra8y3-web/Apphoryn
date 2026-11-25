# Apphoryn self-hosted runtime

## What this deployment gives you

- One long-running FastAPI process with bounded background workers.
- Persistent SQLite files and downloaded artifacts on a named Docker volume.
- Optional self-hosted open-weight inference through Ollama.
- Offline local event capture and later replication to PostgreSQL or Cloudflare D1.
- Health checks and automatic container restart after process or host restart.

This is a self-hosted service, not a free cloud host. It requires a computer, NAS or VPS that remains powered on. Docker restart policies do not keep a powered-off machine online.

## 1. Configure secrets

Copy \`.env.example\` to \`.env\` and set at least:
- \`APPHORYN_ADMIN_TOKEN\`
- \`APPHORYN_INGEST_TOKEN\`
- \`APPHORYN_GITHUB_TOKEN\` only if GitHub Actions dispatch or approved repository pull requests are required.
- The AI provider credentials you actually use.

Do not commit \`.env\`. The provided \`.dockerignore\` excludes it from the image.

The default bind address is \`127.0.0.1\`; this exposes the web UI only on the machine itself. Put it behind a trusted TLS reverse proxy before remote access. Set \`APPHORYN_BIND=0.0.0.0\` only when you have configured firewall rules and authentication.

## 2. Start Apphoryn

\`\`\`bash
docker compose up -d --build
docker compose logs -f apphoryn
\`\`\`

The Docker volume \`apphoryn_data\` stores:
- \`apphoryn.sqlite3\`
- \`agent-workforce.sqlite3\`
- \`analytics.sqlite3\`
- downloaded, verified artifacts

Back up this volume. Do not remove it during normal upgrades. The application container runs as a non-root user.

## 3. Optional local AI with Ollama

Add this line to \`.env\`:

\`\`\`dotenv
OLLAMA_BASE_URL=http://ollama:11434
APPHORYN_AI_PREFER_LOCAL=true
\`\`\`

Start the local-AI profile:

\`\`\`bash
docker compose --profile local-ai up -d
docker compose exec ollama ollama pull qwen2.5-coder:7b
\`\`\`

The model files are kept in the separate \`ollama_models\` volume. Download models while online; after they are present, local inference can continue without Internet access, subject to the host having enough RAM/CPU/GPU and disk space. The default Compose file does not expose Ollama's API port to the public host network.

Ollama CPU inference can be slow. For GPU acceleration, follow the official Ollama Docker instructions for the host's NVIDIA or AMD driver stack; do not add GPU devices to Compose until the host driver/runtime is configured and verified.

## 4. vLLM

For a GPU server using vLLM, run it on the same private Docker network or a loopback endpoint, then set:
- \`VLLM_BASE_URL=http://vllm:8000/v1\`
- \`VLLM_API_KEY\` when the server is configured with a key.

Apphoryn only accepts loopback or explicitly named private Compose hosts for local model endpoints. Never publish an unauthenticated vLLM endpoint to the Internet. Keep it behind a private network/firewall and a trusted authentication proxy.

## 5. Remote database replication

Local analytics is always the durable first write. Optional remote replication is configured by one of these backends:

**Neon or Supabase Postgres**
\`\`\`dotenv
APPHORYN_ANALYTICS_POSTGRES_DSN=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
\`\`\`

**Cloudflare D1**
\`\`\`dotenv
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_D1_DATABASE_ID=...
CLOUDFLARE_D1_API_TOKEN=...
\`\`\`

The D1 API token needs the minimum D1 database query permissions required by the Cloudflare API. Keep it separate from the Cloudflare AI inference token and never commit either credential. Apphoryn creates its analytics table on the first successful sync. Local events remain pending and retry when the connection is restored.

Free plans have quotas, inactivity policies and no production uptime guarantee. Verify current provider limits in your account before relying on them.

## 6. GitHub-hosted ephemeral compute

GitHub Actions is not an always-on server. It creates an ephemeral runner for an approved workflow and shuts it down when the job completes. Standard hosted runners are free for public repositories; private repositories use included account minutes and may incur charges after quota.

To enable Apphoryn's public-source security scan:
- configure a GitHub token with Actions workflow-dispatch permission on the Apphoryn repository;
- configure repository owner/name and ref in \`.env\`;
- set \`APPHORYN_GITHUB_COMPUTE_ENABLED=true\`;
- use the protected dashboard form and explicit per-job approval.

The scan workflow checks out only a public target repository and uses read-only mounts for analysis tools. It does not run target build scripts, package managers, install hooks or GitHub workflows. Reports are uploaded as workflow artifacts. A scan is evidence for review, not a malware-free guarantee.

## 7. AI code changes

An AI change request requires all of these gates:
1. The task's separate \`allow_code_changes\` approval.
2. Server policy \`APPHORYN_CODE_WRITES_ENABLED=true\`.
3. A GitHub token able to create branches, contents and pull requests.
4. Target repository in \`APPHORYN_CODE_WRITE_REPOSITORIES\`, or the configured Apphoryn owner/repository.
5. Target path inside \`APPHORYN_CODE_WRITE_ALLOWLIST\` (default: \`app,tests,scripts,apps/web\`).

The tool writes only to a uniquely named proposal branch and opens a pull request. It does not push directly to the default branch and rejects workflows, environment files, credentials and signing-key formats. Review the diff and CI before merging.

## 8. Offline and failover behavior

When the Internet link is lost but the Apphoryn host and LAN remain powered:
- local UI/API, SQLite task queue, memory and analytics capture continue;
- pre-downloaded Ollama models can continue answering local tasks;
- analytics events wait in the SQLite outbox and sync after connectivity returns;
- GitHub Actions, Gemini/Groq/Grok cloud inference, remote databases and upstream catalogs are unavailable until connectivity returns.

This does not automatically acquire a free VPS or switch to another free cloud host. No general-purpose provider guarantees an always-on free server. For remote 24/7 service, use a host with an explicit uptime and storage plan. A second machine can be configured as a separate replica, but reliable failover requires health checks, replication, backups and a tested endpoint switch; it is not automatic merely because credentials for two providers exist.
