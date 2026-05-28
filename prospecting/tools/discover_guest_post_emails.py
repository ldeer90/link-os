#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.pipeline import discover_emails


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl candidate domains and discover public business emails.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=15)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(discover_emails(args.workers, args.limit, args.max_pages, args.timeout), indent=2))


if __name__ == "__main__":
    main()
