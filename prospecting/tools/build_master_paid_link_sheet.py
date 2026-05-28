#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.paths import REVIEW_DIR


DEFAULT_BASE = REVIEW_DIR / "paid_link_acceptance_review.csv"
DEFAULT_MISSING = REVIEW_DIR / "missing_email_discovery_review.csv"
DEFAULT_APIFY = REVIEW_DIR / "missing_email_discovery_review_apify_top100.csv"
DEFAULT_OUTPUT = REVIEW_DIR / "master_paid_link_review_sheet.csv"
DEFAULT_SUMMARY = REVIEW_DIR / "master_paid_link_review_sheet_summary.json"

MASTER_EXTRA_FIELDS = [
    "best_contact_email",
    "best_contact_email_type",
    "best_contact_source",
    "best_contact_method",
    "best_contact_is_verified_public",
    "best_contact_is_role_guess",
    "best_contact_next_action",
    "all_public_discovered_emails",
    "all_public_discovered_email_sources",
    "all_role_pattern_guess_emails",
    "all_role_pattern_guess_types",
    "discovery_public_email_count",
    "discovery_role_guess_count",
    "discovery_total_candidate_count",
    "has_any_contact_candidate",
]

CONTACT_RANK = {
    "advertising": 0,
    "media": 1,
    "partnerships": 2,
    "sponsored": 3,
    "editor": 4,
    "content": 5,
    "generic": 6,
    "": 7,
}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def uniq(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = (value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def join(values: list[str]) -> str:
    return " | ".join(uniq(values))


def candidate_sort_key(row: dict[str, str]) -> tuple[int, int, int, str]:
    is_guess = row.get("is_role_pattern_guess") == "yes"
    is_public = row.get("is_verified_public_email") == "yes"
    recommended = row.get("recommended_contact") == "yes"
    return (
        1 if is_guess else 0,
        0 if recommended else 1,
        0 if is_public else 1,
        str(CONTACT_RANK.get(row.get("email_type", ""), 9)),
    )


def best_base_contact(row: dict[str, str]) -> dict[str, str] | None:
    email = (row.get("email") or "").strip()
    if not email:
        return None
    return {
        "email": email,
        "email_type": row.get("email_type", ""),
        "email_source_url": row.get("email_source_url", ""),
        "email_source_method": "original_paid_link_review",
        "is_verified_public_email": "yes",
        "is_role_pattern_guess": "no",
        "recommended_next_action": row.get("recommended_next_action", ""),
        "recommended_contact": "yes",
    }


def build_master_rows(base_rows: list[dict[str, str]], discovery_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_domain: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_discovery: set[tuple[str, str, str, str]] = set()
    for row in discovery_rows:
        key = (
            row.get("root_domain", ""),
            row.get("email", ""),
            row.get("email_source_url", ""),
            row.get("email_source_method", ""),
        )
        if row.get("root_domain") and row.get("email") and key not in seen_discovery:
            seen_discovery.add(key)
            by_domain[row["root_domain"]].append(row)

    output: list[dict[str, str]] = []
    for base in base_rows:
        domain = base.get("root_domain", "")
        candidates = list(by_domain.get(domain, []))
        original = best_base_contact(base)
        if original:
            candidates.append(original)
        sorted_candidates = sorted(candidates, key=candidate_sort_key)
        best = sorted_candidates[0] if sorted_candidates else {}
        public = [row for row in candidates if row.get("is_verified_public_email") == "yes" and row.get("is_role_pattern_guess") != "yes"]
        guesses = [row for row in candidates if row.get("is_role_pattern_guess") == "yes"]
        merged = dict(base)
        merged.update(
            {
                "best_contact_email": best.get("email", ""),
                "best_contact_email_type": best.get("email_type", ""),
                "best_contact_source": best.get("email_source_url", ""),
                "best_contact_method": best.get("email_source_method", ""),
                "best_contact_is_verified_public": best.get("is_verified_public_email", ""),
                "best_contact_is_role_guess": best.get("is_role_pattern_guess", ""),
                "best_contact_next_action": best.get("recommended_next_action", ""),
                "all_public_discovered_emails": join([row.get("email", "") for row in public]),
                "all_public_discovered_email_sources": join([row.get("email_source_url", "") for row in public]),
                "all_role_pattern_guess_emails": join([row.get("email", "") for row in guesses]),
                "all_role_pattern_guess_types": join([row.get("email_type", "") for row in guesses]),
                "discovery_public_email_count": str(len(uniq([row.get("email", "") for row in public]))),
                "discovery_role_guess_count": str(len(uniq([row.get("email", "") for row in guesses]))),
                "discovery_total_candidate_count": str(len(uniq([row.get("email", "") for row in candidates]))),
                "has_any_contact_candidate": "yes" if candidates else "no",
            }
        )
        output.append(merged)
    return output


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one master paid-link review sheet with all contact discovery columns merged.")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--missing", type=Path, default=DEFAULT_MISSING)
    parser.add_argument("--apify", type=Path, default=DEFAULT_APIFY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    base_rows = read_rows(args.base)
    discovery_rows = read_rows(args.missing) + read_rows(args.apify)
    master_rows = build_master_rows(base_rows, discovery_rows)
    base_fields = list(base_rows[0].keys()) if base_rows else []
    fields = MASTER_EXTRA_FIELDS + [field for field in base_fields if field not in MASTER_EXTRA_FIELDS]
    write_csv(args.output, master_rows, fields)
    summary = {
        "base_rows": len(base_rows),
        "discovery_rows": len(discovery_rows),
        "output_rows": len(master_rows),
        "rows_with_existing_email": sum(1 for row in base_rows if row.get("email")),
        "rows_with_best_contact": sum(1 for row in master_rows if row.get("best_contact_email")),
        "rows_with_public_discovered_email": sum(1 for row in master_rows if row.get("all_public_discovered_emails")),
        "rows_with_role_pattern_guess": sum(1 for row in master_rows if row.get("all_role_pattern_guess_emails")),
        "best_contact_methods": dict(Counter(row.get("best_contact_method", "") for row in master_rows if row.get("best_contact_method"))),
        "output": str(args.output),
        "summary": str(args.summary),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
