#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.apify_google import ApifyGoogleHarvestConfig, harvest_apify_google_to_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harvest real Google operator results through Apify to CSV without writing SQLite.")
    parser.add_argument("--query-offset", type=int, default=0)
    parser.add_argument("--query-limit", type=int, default=25)
    parser.add_argument("--pages-per-query", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--raw-output", type=Path)
    parser.add_argument("--country-code", default="au")
    parser.add_argument("--language-code", default="en")
    parser.add_argument("--wait-timeout-seconds", type=int, default=180)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = harvest_apify_google_to_csv(
        ApifyGoogleHarvestConfig(
            query_offset=args.query_offset,
            query_limit=args.query_limit,
            pages_per_query=args.pages_per_query,
            batch_size=args.batch_size,
            output=args.output,
            raw_output=args.raw_output,
            country_code=args.country_code,
            language_code=args.language_code,
            wait_timeout_seconds=args.wait_timeout_seconds,
            delay_seconds=args.delay_seconds,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
