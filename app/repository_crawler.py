from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os


class RepositoryCrawlError(RuntimeError):
    pass


class RepositoryCrawler:
    """Incremental cursor over GitHub's public-repository listing endpoint.

    GitHub's GET /repositories endpoint enumerates public repositories by
    monotonically increasing repository ID. The since cursor has no
    search-result 1,000-item ceiling, includes public forks, and can resume
    from the last completely consumed page.
    """

    PAGE_SIZE = 100

    def __init__(self, github: Any, state_path: str | Path):
        self.github = github
        self.state_path = Path(state_path)
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": 2,
                "last_repository_id": 0,
                "totals": {
                    "repository_pages": 0,
                    "repositories_seen": 0,
                    "empty_tail_checks": 0,
                },
            }
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryCrawlError(f"Could not read Scout checkpoint: {exc}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != 2:
            raise RepositoryCrawlError("Scout checkpoint has an unsupported schema version.")
        cursor = state.get("last_repository_id")
        if not isinstance(cursor, int) or cursor < 0:
            raise RepositoryCrawlError("Scout checkpoint repository ID is invalid.")
        state.setdefault("totals", {})
        return state

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def scan(
        self,
        max_repositories: int = 200,
        max_pages: int = 25,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        max_repositories = max(1, min(int(max_repositories), 10000))
        max_pages = max(1, min(int(max_pages), 100))
        records: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        pages = 0
        cursor = int(self.state["last_repository_id"])
        reached_current_end = False

        while len(records) < max_repositories and pages < max_pages:
            page = self.github.list_public_repositories(since=cursor, per_page=self.PAGE_SIZE)
            pages += 1
            if not isinstance(page, list):
                raise RepositoryCrawlError("GitHub public repository endpoint returned a non-list response.")
            if not page:
                reached_current_end = True
                self.state["totals"]["empty_tail_checks"] = int(
                    self.state["totals"].get("empty_tail_checks", 0)
                ) + 1
                break

            valid_rows: list[tuple[int, dict[str, Any]]] = []
            for item in page:
                if not isinstance(item, dict):
                    continue
                try:
                    repository_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if repository_id > cursor:
                    valid_rows.append((repository_id, item))

            valid_rows.sort(key=lambda pair: pair[0])
            if not valid_rows:
                raise RepositoryCrawlError(
                    f"GitHub returned no repository ID greater than since={cursor}; "
                    "the cursor was not advanced to avoid silently skipping repositories."
                )

            # Consume the complete API page before advancing the since cursor.
            # Batch limits can be exceeded by at most PAGE_SIZE - 1 results;
            # truncating a page would permanently discard the remaining IDs.
            for repository_id, item in valid_rows:
                if repository_id in seen_ids:
                    continue
                seen_ids.add(repository_id)
                candidate = dict(item)
                candidate["scout_repository_id"] = repository_id
                candidate["scout_query"] = f"GET /repositories?since={cursor}"
                records.append(candidate)

            cursor = max(repository_id for repository_id, _ in valid_rows)
            self.state["last_repository_id"] = cursor
            self.state["totals"]["repository_pages"] = int(
                self.state["totals"].get("repository_pages", 0)
            ) + 1
            if len(page) < self.PAGE_SIZE:
                reached_current_end = True
                break

        self.state["last_repository_id"] = cursor
        self.state["totals"]["repositories_seen"] = int(
            self.state["totals"].get("repositories_seen", 0)
        ) + len(records)
        report = {
            "repositories": len(records),
            "repository_pages": pages,
            "last_repository_id": cursor,
            "reached_current_end": reached_current_end,
            "totals": dict(self.state["totals"]),
        }
        return records, report
