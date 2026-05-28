#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.search_harvest import import_harvest_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import search-operator harvest CSVs into candidate tables and crawl queue.")
    parser.add_argument("csvs", nargs="+", type=Path)
    parser.add_argument("--source-label", default="search_harvest_csv")
    parser.add_argument("--no-reset-crawl", action="store_true", help="Do not requeue imported result URLs for crawling.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(
        json.dumps(
            import_harvest_csv(args.csvs, source_label=args.source_label, reset_crawl=not args.no_reset_crawl),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
