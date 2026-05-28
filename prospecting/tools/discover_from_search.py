#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import discover_from_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover .com.au guest-post candidates from broad free search scraping.")
    parser.add_argument("--search-limit", type=int, default=1000)
    parser.add_argument("--pages-per-query", type=int, default=2)
    parser.add_argument("--query-limit", type=int, default=0, help="Limit generated queries for test/sample runs.")
    parser.add_argument("--query-offset", type=int, default=0, help="Skip this many generated queries before running.")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=int, default=6)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = discover_from_search(
        search_limit=args.search_limit,
        pages_per_query=args.pages_per_query,
        refresh=args.refresh,
        delay_seconds=args.delay_seconds,
        query_limit=args.query_limit,
        query_offset=args.query_offset,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
