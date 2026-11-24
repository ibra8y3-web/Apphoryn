from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Tiny dotenv loader; process environment always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APPHORYN_NAME", "Apphoryn")
    database_path: Path = Path(os.getenv("APPHORYN_DATABASE", str(ROOT / "storage" / "apphoryn.sqlite3")))
    storage_path: Path = Path(os.getenv("APPHORYN_STORAGE", str(ROOT / "storage" / "artifacts")))
    github_token: str = os.getenv("APPHORYN_GITHUB_TOKEN", "")
    admin_token: str = os.getenv("APPHORYN_ADMIN_TOKEN", "")
    ingest_token: str = os.getenv("APPHORYN_INGEST_TOKEN", "")
    github_owner: str = os.getenv("APPHORYN_GITHUB_OWNER", "")
    github_repository: str = os.getenv("APPHORYN_GITHUB_REPOSITORY", "")
    github_ref: str = os.getenv("APPHORYN_GITHUB_REF", "main")
    build_workflow: str = os.getenv("APPHORYN_BUILD_WORKFLOW", "build-app.yml")
    public_base_url: str = os.getenv("APPHORYN_PUBLIC_BASE_URL", "http://localhost:8000")
    max_artifact_bytes: int = int(os.getenv("APPHORYN_MAX_ARTIFACT_BYTES", str(1024 * 1024 * 1024)))
    max_import_bytes: int = int(os.getenv("APPHORYN_MAX_IMPORT_BYTES", str(512 * 1024 * 1024)))
    ai_base_url: str = os.getenv("APPHORYN_AI_BASE_URL", "https://api.openai.com/v1")
    ai_api_key: str = os.getenv("APPHORYN_AI_API_KEY", "")
    ai_model: str = os.getenv("APPHORYN_AI_MODEL", "")


settings = Settings()
settings.database_path.parent.mkdir(parents=True, exist_ok=True)
settings.storage_path.mkdir(parents=True, exist_ok=True)
