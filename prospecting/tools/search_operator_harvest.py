#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.search_harvest import HarvestConfig, harvest_search_to_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harvest broad search-operator results to CSV without writing SQLite.")
    parser.add_argument("--query-offset", type=int, default=0)
    parser.add_argument("--query-limit", type=int, default=300)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=3)
    parser.add_argument("--pages-per-query", type=int, default=1)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--engine", choices=["google", "bing", "duckduckgo", "all"], default="google")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = harvest_search_to_csv(
        HarvestConfig(
            query_offset=args.query_offset,
            query_limit=args.query_limit,
            workers=args.workers,
            timeout=args.timeout,
            pages_per_query=args.pages_per_query,
            refresh=args.refresh,
            delay_seconds=args.delay_seconds,
            output=args.output,
            engine=args.engine,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
