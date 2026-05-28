#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import crawl_and_score_universe, crawl_and_score_universe_parallel, enqueue_candidate_urls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl queued universe candidate URLs and score confirmed evidence.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--delay-seconds", type=float, default=0.5)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers for faster iteration.")
    parser.add_argument("--enqueue-candidates", action="store_true", help="Queue current candidate URLs before crawling.")
    parser.add_argument("--enqueue-limit", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    enqueued = enqueue_candidate_urls(args.enqueue_limit) if args.enqueue_candidates else 0
    if args.workers > 1:
        result = crawl_and_score_universe_parallel(
            limit=args.limit,
            retry_errors=args.retry_errors,
            timeout=args.timeout,
            workers=args.workers,
        )
    else:
        result = crawl_and_score_universe(
            limit=args.limit,
            retry_errors=args.retry_errors,
            timeout=args.timeout,
            delay_seconds=args.delay_seconds,
        )
    result["enqueued_before_crawl"] = enqueued
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
