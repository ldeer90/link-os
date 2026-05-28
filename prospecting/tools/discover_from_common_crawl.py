#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import discover_from_common_crawl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover .com.au guest-post candidates from the free Common Crawl URL index.")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--index-id", default="", help="Common Crawl index id, e.g. CC-MAIN-2026-18. Defaults to latest.")
    parser.add_argument("--pattern-limit", type=int, default=0, help="Limit URL patterns for sample runs.")
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(
        json.dumps(
            discover_from_common_crawl(
                limit=args.limit,
                index_id=args.index_id,
                pattern_limit=args.pattern_limit,
                delay_seconds=args.delay_seconds,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
