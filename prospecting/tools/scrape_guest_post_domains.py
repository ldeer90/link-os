#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.pipeline import scrape_domains


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape search result pages for AU guest-post prospect domains.")
    parser.add_argument("--search-limit", type=int, default=250, help="Maximum unique domains to save.")
    parser.add_argument("--pages-per-query", type=int, default=2)
    parser.add_argument("--refresh", action="store_true", help="Ignore cached search HTML.")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(
        json.dumps(
            scrape_domains(
                search_limit=args.search_limit,
                pages_per_query=args.pages_per_query,
                refresh=args.refresh,
                delay_seconds=args.delay_seconds,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
