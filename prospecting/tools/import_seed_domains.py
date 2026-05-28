#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import import_seed_domains


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import known .com.au seed domains/URLs into the universe crawl queue.")
    parser.add_argument("items", nargs="*", help="Domains or URLs, e.g. tagg.com.au or https://tagg.com.au/advertising/")
    parser.add_argument("--input", type=Path, help="Optional newline file with domains or URLs.")
    parser.add_argument("--source-label", default="manual_seed")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    items = list(args.items)
    if args.input:
        items.extend(line.strip() for line in args.input.read_text(encoding="utf-8").splitlines())
    print(json.dumps(import_seed_domains(items, source_label=args.source_label), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
