#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import probe_candidate_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue known guest-post/media-kit paths for discovered .com.au candidate domains.")
    parser.add_argument("--domain-limit", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(probe_candidate_paths(domain_limit=args.domain_limit), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
