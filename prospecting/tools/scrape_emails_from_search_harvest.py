#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.search_email_scrape import EmailScrapeConfig, scrape_emails_from_harvest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape emails from domains found in search-operator harvest CSVs.")
    parser.add_argument("csvs", nargs="+", type=Path)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=4)
    parser.add_argument("--max-pages-per-domain", type=int, default=6)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = scrape_emails_from_harvest(
        EmailScrapeConfig(
            harvest_csvs=args.csvs,
            workers=args.workers,
            timeout=args.timeout,
            max_pages_per_domain=args.max_pages_per_domain,
            output=args.output,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
