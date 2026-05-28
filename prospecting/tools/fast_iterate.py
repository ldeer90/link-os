#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import (
    crawl_and_score_universe_parallel,
    expand_from_confirmed_sites,
    export_universe,
    import_cached_search_results,
    probe_candidate_paths,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast local iteration loop: harvest cache, crawl in parallel, expand, export.")
    parser.add_argument("--crawl-limit", type=int, default=300)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=3)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--skip-cache-import", action="store_true")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument("--skip-expand", action="store_true")
    parser.add_argument("--probe-domain-limit", type=int, default=0)
    parser.add_argument("--expand-domain-limit", type=int, default=80)
    parser.add_argument("--expand-per-domain-limit", type=int, default=20)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    started = time.time()
    results: dict[str, object] = {}
    if not args.skip_cache_import:
        results["cache_import"] = import_cached_search_results()
    if not args.skip_probe:
        results["path_probe"] = probe_candidate_paths(domain_limit=args.probe_domain_limit)
    results["parallel_crawl"] = crawl_and_score_universe_parallel(
        limit=args.crawl_limit,
        retry_errors=args.retry_errors,
        timeout=args.timeout,
        workers=args.workers,
    )
    if not args.skip_expand:
        results["expansion"] = expand_from_confirmed_sites(
            domain_limit=args.expand_domain_limit,
            per_domain_limit=args.expand_per_domain_limit,
        )
    results["export"] = export_universe()
    results["elapsed_seconds"] = round(time.time() - started, 2)
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
