#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import expand_from_confirmed_sites


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover adjacent .com.au publisher sites from confirmed candidates' outbound links.")
    parser.add_argument("--domain-limit", type=int, default=0)
    parser.add_argument("--per-domain-limit", type=int, default=25)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(
        json.dumps(
            expand_from_confirmed_sites(domain_limit=args.domain_limit, per_domain_limit=args.per_domain_limit),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
