from __future__ import annotations

import argparse
import json

from .services import CatalogService


def main() -> None:
    parser = argparse.ArgumentParser(description="Apphoryn GitHub discovery agent")
    parser.add_argument("--query", default="topic:opensource stars:>20", help="GitHub repository search query")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    items = CatalogService().discover(args.query, max(1, min(args.limit, 50)))
    print(json.dumps(items, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
