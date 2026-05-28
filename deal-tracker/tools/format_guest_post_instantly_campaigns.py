#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.instantly import request  # noqa: E402


DEFAULT_CAMPAIGN_IDS = [
    "945b0afd-7c53-492d-9afe-4c44f57173ad",
    "7bc7d5e3-924d-463b-b875-8e8301be264b",
    "48508c88-901d-4ba7-963b-031514a9ef73",
    "1d924dab-2fb9-4d2c-89f3-0744ebe9b065",
]

SUBJECT = "Article Submission Info Request"

BODY_PARAGRAPHS = [
    [
        "Hi {{firstName}},",
        "I'm Laurence Deer. I manage SEO and content promotion for a large portfolio of Australian ecommerce stores.",
        "I wanted to ask whether {{site_name}} accepts article submissions, sponsored articles, guest posts, advertorials, or similar editorial placements.",
        "If so, could you let me know whether there are any fees, topic guidelines, link requirements, or other conditions?",
        "Regards,",
        "Laurence",
        "LD Search",
        "ldsearch.com.au",
        'If this is not relevant, just reply "no" and I won\'t follow up.',
    ],
    [
        "Hi {{firstName}},",
        "Just following up on this.",
        "I'm looking for the right process for article submissions or sponsored/editorial placements on {{site_name}}.",
        "Is there a rate card, media kit, or contributor guideline page I should look at?",
        "Regards,",
        "Laurence",
    ],
    [
        "Hi {{firstName}},",
        "Quick nudge from me.",
        "We work with Australian ecommerce brands and often need relevant sites for useful content placements.",
        "Do you accept paid article submissions or sponsored content, and what are the usual fees or requirements?",
        "Regards,",
        "Laurence",
    ],
    [
        "Hi {{firstName}},",
        "Is there someone else who handles article submissions, advertising, or sponsored content enquiries for {{site_name}}?",
        "If yes, could you point me in the right direction?",
        "Regards,",
        "Laurence",
    ],
    [
        "Hi {{firstName}},",
        "I'm still trying to confirm whether {{site_name}} offers any paid editorial, guest post, sponsored article, or content placement options.",
        'Even a short reply with "yes, send details" or "no, we don\'t offer this" would be helpful.',
        "Regards,",
        "Laurence",
    ],
    [
        "Hi {{firstName}},",
        "Last follow-up from me.",
        "If {{site_name}} does accept paid article submissions or content placements, could you send through the fees and requirements?",
        "If not, no worries at all.",
        "Regards,",
        "Laurence",
    ],
]


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def div_body(paragraphs: list[str]) -> str:
    blocks: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        blocks.append(f"<div>{escape_html(paragraph)}</div>")
        if index != len(paragraphs) - 1:
            blocks.append("<div><br /></div>")
    return "".join(blocks)


def format_sequences(sequences: list[dict]) -> list[dict]:
    formatted = copy.deepcopy(sequences)
    steps = formatted[0].get("steps") if formatted else []
    if len(steps) < len(BODY_PARAGRAPHS):
        raise RuntimeError(f"Expected at least {len(BODY_PARAGRAPHS)} steps, got {len(steps)}")
    for index, paragraphs in enumerate(BODY_PARAGRAPHS):
        step = steps[index]
        variants = step.get("variants") or []
        if not variants:
            raise RuntimeError(f"Step {index + 1} has no variants")
        for variant in variants:
            variant["subject"] = SUBJECT
            variant["body"] = div_body(paragraphs)
    return formatted


def main() -> None:
    parser = argparse.ArgumentParser(description="Format guest-post Instantly campaign bodies with SEO-audit-style HTML spacing.")
    parser.add_argument("--campaign-id", action="append", dest="campaign_ids")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    campaign_ids = args.campaign_ids or DEFAULT_CAMPAIGN_IDS
    for campaign_id in campaign_ids:
        campaign = request(f"/campaigns/{campaign_id}")
        sequences = campaign.get("sequences") or []
        formatted = format_sequences(sequences)
        print(f"{campaign_id} | {campaign.get('name')} | steps={len(formatted[0].get('steps') or [])}")
        if args.dry_run:
            print((formatted[0]["steps"][0]["variants"][0]["body"])[:260])
            continue
        request(f"/campaigns/{campaign_id}", method="PATCH", body={"sequences": formatted})
        print("  updated")


if __name__ == "__main__":
    main()
