#!/usr/bin/env python3
"""Backfill stored anchor-intent evidence without spending provider credits."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict

from sqlalchemy import select

from link_os_native import configure_native_defaults


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    configure_native_defaults()

    from deal_tracker.anchor_intent import aggregate_anchor_intent
    from deal_tracker.platform.models import AuditLog, BacklinkCandidate, BacklinkEvidence
    from deal_tracker.platform_runtime import session_factory

    counts: Counter[str] = Counter()
    changed = 0
    with session_factory()() as session:
        candidates = list(session.scalars(select(BacklinkCandidate).order_by(BacklinkCandidate.id)))
        evidence_by_candidate: dict[tuple[str, str], list[BacklinkEvidence]] = defaultdict(list)
        for row in session.scalars(select(BacklinkEvidence).order_by(BacklinkEvidence.id)):
            evidence_by_candidate[(row.run_id, row.source_domain)].append(row)
        for candidate in candidates:
            evidence = evidence_by_candidate.get((candidate.run_id, candidate.normalized_domain), [])
            assessment = aggregate_anchor_intent(evidence)
            counts[assessment.classification] += 1
            if (
                candidate.commercial_anchor_class != assessment.classification
                or candidate.commercial_anchor_score != assessment.score
                or candidate.commercial_anchor_signals != list(assessment.signals)
            ):
                changed += 1
                if args.execute:
                    candidate.commercial_anchor_class = assessment.classification
                    candidate.commercial_anchor_score = assessment.score
                    candidate.commercial_anchor_signals = list(assessment.signals)
        if args.execute:
            session.add(
                AuditLog(
                    actor_type="system",
                    actor_id="anchor-intent-backfill",
                    action="backlink_anchor_intent_backfilled",
                    entity_type="backlink_candidates",
                    entity_id="all",
                    after={"changed": changed, "counts": dict(sorted(counts.items())), "provider_credits": 0},
                )
            )
            session.commit()
    print(json.dumps({"mode": "execute" if args.execute else "dry_run", "candidates": sum(counts.values()), "changed": changed, "counts": dict(sorted(counts.items())), "provider_credits": 0}))


if __name__ == "__main__":
    main()
