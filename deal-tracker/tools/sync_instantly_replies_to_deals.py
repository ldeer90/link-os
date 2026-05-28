#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deal_tracker.instantly import DEFAULT_CAMPAIGN_IDS
from deal_tracker.deals import sync_replies
from deal_tracker.sync_state import run_sync_recorded


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Instantly inbound replies into the link deals database.")
    parser.add_argument("--campaign-id", action="append", dest="campaign_ids")
    args = parser.parse_args()
    result = sync_replies(args.campaign_ids or DEFAULT_CAMPAIGN_IDS) if args.campaign_ids else run_sync_recorded("cli")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
