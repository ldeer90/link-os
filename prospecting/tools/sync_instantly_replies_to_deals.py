#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.deals import DEFAULT_CAMPAIGN_IDS, sync_replies


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Instantly inbound replies into the link deals database.")
    parser.add_argument(
        "--campaign-id",
        action="append",
        dest="campaign_ids",
        help="Campaign ID to sync. Can be supplied multiple times. Defaults to active and old guest post campaigns.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = sync_replies(args.campaign_ids or DEFAULT_CAMPAIGN_IDS)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
