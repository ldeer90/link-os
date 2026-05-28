#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.apify_google import load_env_value
from guest_post_prospecting.missing_email_discovery import (
    MISSING_EMAIL_REVIEW_CSV,
    MISSING_EMAIL_SUMMARY_JSON,
    MissingEmailConfig,
    discover_missing_emails,
)
from guest_post_prospecting.paid_link_acceptance import PAID_LINK_REVIEW_CSV


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find public emails for paid-link prospects missing contact emails.")
    parser.add_argument("--input", type=Path, default=PAID_LINK_REVIEW_CSV)
    parser.add_argument("--output", type=Path, default=MISSING_EMAIL_REVIEW_CSV)
    parser.add_argument("--summary", type=Path, default=MISSING_EMAIL_SUMMARY_JSON)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--max-domains", type=int, default=0)
    parser.add_argument("--include-out-of-scope", action="store_true")
    parser.add_argument("--no-apify-search", action="store_true")
    parser.add_argument("--no-public-web-brand-search", action="store_true")
    parser.add_argument("--no-scrape-social-profiles", action="store_true")
    parser.add_argument("--no-role-pattern-guesses", action="store_true")
    parser.add_argument("--max-pages-per-domain", type=int, default=30)
    parser.add_argument("--max-apify-domains", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    has_apify_token = bool(load_env_value("APIFY_API_TOKEN") or load_env_value("APIFY_TOKEN"))
    result = discover_missing_emails(
        MissingEmailConfig(
            input=args.input,
            output=args.output,
            summary=args.summary,
            workers=args.workers,
            timeout=args.timeout,
            max_domains=args.max_domains,
            include_out_of_scope=args.include_out_of_scope,
            use_apify_search=has_apify_token and not args.no_apify_search,
            public_web_brand_search=not args.no_public_web_brand_search,
            scrape_social_profiles=not args.no_scrape_social_profiles,
            role_pattern_guesses=not args.no_role_pattern_guesses,
            max_pages_per_domain=args.max_pages_per_domain,
            max_apify_domains=args.max_apify_domains,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
