#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.paid_link_acceptance import (
    DEFAULT_DETAILED_REVIEW_CSV,
    PAID_LINK_REVIEW_CSV,
    PAID_LINK_SUMMARY_JSON,
    score_paid_link_file,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score domains by likelihood they will accept money for a link.")
    parser.add_argument("--input", type=Path, default=DEFAULT_DETAILED_REVIEW_CSV)
    parser.add_argument("--output", type=Path, default=PAID_LINK_REVIEW_CSV)
    parser.add_argument("--summary", type=Path, default=PAID_LINK_SUMMARY_JSON)
    args = parser.parse_args()
    result = score_paid_link_file(args.input, args.output, args.summary)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
