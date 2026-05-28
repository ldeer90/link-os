#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.instantly_campaigns import (
    DEFAULT_SENDER,
    DEFAULT_SUBJECT,
    MASTER_PAID_LINK_SHEET,
    CampaignBuildConfig,
    build_or_create_campaign,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or create the Article Submission Info Request Instantly campaign.")
    parser.add_argument("--input", type=Path, default=MASTER_PAID_LINK_SHEET)
    parser.add_argument("--campaign-name", default="Article Submission Info Request | AU Paid Link Sites")
    parser.add_argument("--subject", default=DEFAULT_SUBJECT)
    parser.add_argument("--sender", default=DEFAULT_SENDER)
    parser.add_argument("--execute", action="store_true", help="Create the campaign and upload leads via Instantly API.")
    parser.add_argument("--exclude-out-of-scope", action="store_true")
    parser.add_argument("--exclude-role-pattern-guesses", action="store_true")
    parser.add_argument("--max-leads", type=int, default=0)
    parser.add_argument("--daily-max-leads", type=int, default=0)
    parser.add_argument("--email-gap-minutes", type=int, default=45)
    parser.add_argument("--random-wait-max-minutes", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=1000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = build_or_create_campaign(
        CampaignBuildConfig(
            input=args.input,
            campaign_name=args.campaign_name,
            subject=args.subject,
            sender=args.sender,
            execute=args.execute,
            include_out_of_scope=not args.exclude_out_of_scope,
            include_role_pattern_guesses=not args.exclude_role_pattern_guesses,
            max_leads=args.max_leads,
            daily_max_leads=args.daily_max_leads,
            email_gap_minutes=args.email_gap_minutes,
            random_wait_max_minutes=args.random_wait_max_minutes,
            batch_size=args.batch_size,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
