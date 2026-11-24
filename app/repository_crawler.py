from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any


class RepositoryCrawlError(RuntimeError):
    pass


class RepositoryCrawler:
    """Incremental, resumable GitHub repository crawler.

    Search API result windows are capped at 1,000 records. Dense windows are
    split into non-overlapping date, star, fork and size ranges before paging.
    The state file is the durable cursor; failed runs can safely replay a page
    because catalogue writes and artifact mirrors are idempotent.
    """

    PAGE_SIZE = 100
    SEARCH_WINDOW_LIMIT = 1000
    STAR_BUCKETS = ("0", "1..10", "11..100", "101..1000", "1001..10000", ">10000")
    FORK_BUCKETS = ("0", "1..10", "11..100", "101..1000", ">1000")
    SIZE_BUCKETS = ("0..100", "101..1000", "1001..10000", "10001..100000", ">100000")

    def __init__(self, github: Any, state_path: str | Path, start_date: str = "2008-01-01"):
        self.github = github
        self.state_path = Path(state_path)
        self.start_date = date.fromisoformat(start_date)
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": 1,
                "next_created_date": self.start_date.isoformat(),
                "active_window": None,
                "pending": [],
                "totals": {"search_requests": 0, "repositories_seen": 0, "completed_windows": 0},
            }
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryCrawlError(f"Could not read Scout checkpoint: {exc}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise RepositoryCrawlError("Scout checkpoint has an unsupported schema version.")
        state.setdefault("pending", [])
        state.setdefault("active_window", None)
        state.setdefault("totals", {})
        return state

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _ensure_partition(self, today: date) -> bool:
        if self.state["pending"]:
            return True
        active = self.state.get("active_window")
        if active:
            self.state["next_created_date"] = (date.fromisoformat(active["end"]) + timedelta(days=1)).isoformat()
            self.state["active_window"] = None
            self.state["totals"]["completed_windows"] = int(self.state["totals"].get("completed_windows", 0)) + 1

        start = date.fromisoformat(self.state["next_created_date"])
        if start > today:
            return False
        end = min(start + timedelta(days=29), today)
        self.state["active_window"] = {"start": start.isoformat(), "end": end.isoformat()}
        self.state["pending"] = [{
            "created_from": start.isoformat(),
            "created_to": end.isoformat(),
            "stars": None,
            "forks": None,
            "size": None,
            "page": 1,
            "total_count": None,
        }]
        return True

    @staticmethod
    def _qualifier(value: str | None, name: str) -> str:
        return f"{name}:{value}" if value is not None else ""

    def _query(self, partition: dict[str, Any]) -> str:
        clauses = [
            f"created:{partition['created_from']}..{partition['created_to']}",
            self._qualifier(partition.get("stars"), "stars"),
            self._qualifier(partition.get("forks"), "forks"),
            self._qualifier(partition.get("size"), "size"),
        ]
        return " ".join(item for item in clauses if item)

    def _split_dense_partition(self, partition: dict[str, Any], total_count: int) -> list[dict[str, Any]]:
        start = date.fromisoformat(partition["created_from"])
        end = date.fromisoformat(partition["created_to"])
        days = (end - start).days
        if days > 0:
            midpoint = start + timedelta(days=days // 2)
            ranges = [(start, midpoint), (midpoint + timedelta(days=1), end)]
            return [{
                **partition,
                "created_from": left.isoformat(),
                "created_to": right.isoformat(),
                "page": 1,
                "total_count": None,
            } for left, right in ranges]

        if partition.get("stars") is None:
            values = self.STAR_BUCKETS
            field = "stars"
        elif partition.get("forks") is None:
            values = self.FORK_BUCKETS
            field = "forks"
        elif partition.get("size") is None:
            values = self.SIZE_BUCKETS
            field = "size"
        else:
            query = self._query(partition)
            raise RepositoryCrawlError(
                f"GitHub still reports {total_count} repositories for a one-day leaf partition "
                f"that cannot be safely split further: {query}. The crawler stopped instead of "
                "silently skipping results."
            )
        return [{**partition, field: value, "page": 1, "total_count": None} for value in values]

    def scan(
        self,
        max_repositories: int = 200,
        max_search_requests: int = 25,
        today: date | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        today = today or date.today()
        max_repositories = max(1, min(int(max_repositories), 5000))
        max_search_requests = max(1, min(int(max_search_requests), 100))
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        requests = 0
        split_count = 0

        while len(records) < max_repositories and requests < max_search_requests:
            if not self._ensure_partition(today):
                break

            partition = self.state["pending"][0]
            query = self._query(partition)
            result = self.github.search_repositories(
                query=query,
                page=int(partition.get("page") or 1),
                per_page=self.PAGE_SIZE,
                sort="created",
                order="asc",
            )
            requests += 1
            total_count = int(result.get("total_count") or 0)
            self.state["totals"]["search_requests"] = int(self.state["totals"].get("search_requests", 0)) + 1

            if total_count >= self.SEARCH_WINDOW_LIMIT:
                children = self._split_dense_partition(partition, total_count)
                self.state["pending"][:1] = children
                split_count += 1
                continue

            partition["total_count"] = total_count
            for item in result.get("items") or []:
                if not isinstance(item, dict):
                    continue
                repo_id = str(item.get("id") or "")
                full_name = str(item.get("full_name") or "").casefold()
                key = repo_id or full_name
                if not key or key in seen_ids or (full_name and full_name in seen_names):
                    continue
                seen_ids.add(key)
                if full_name:
                    seen_names.add(full_name)
                item = dict(item)
                item["scout_query"] = query
                records.append(item)
                if len(records) >= max_repositories:
                    break

            page = int(partition.get("page") or 1)
            partition["page"] = page + 1
            if page * self.PAGE_SIZE >= total_count or page >= 10 or not result.get("items"):
                self.state["pending"].pop(0)

        self.state["totals"]["repositories_seen"] = int(self.state["totals"].get("repositories_seen", 0)) + len(records)
        report = {
            "repositories": len(records),
            "search_requests": requests,
            "dense_partitions_split": split_count,
            "next_created_date": self.state["next_created_date"],
            "pending_partitions": len(self.state["pending"]),
            "active_window": self.state.get("active_window"),
            "totals": dict(self.state["totals"]),
        }
        return records, report
