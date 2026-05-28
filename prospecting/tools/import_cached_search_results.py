#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import import_cached_search_results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import cached search HTML files into the universe candidate tables.")
    parser.add_argument("--limit-files", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(import_cached_search_results(limit_files=args.limit_files), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
