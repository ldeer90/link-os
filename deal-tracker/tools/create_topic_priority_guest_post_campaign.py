#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.instantly import ACTIVE_CAMPAIGN_ID, DEFAULT_CAMPAIGN_IDS, list_campaign_leads, request  # noqa: E402
from tools.import_csv_prospects_to_instantly import clean_domain  # noqa: E402


DEFAULT_INPUT = ROOT / "generated" / "imports" / "likely_guest_post_candidates_strict.csv"
DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "topic_priority_guest_post_campaign_import.csv"

EMAIL_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9._%+-]{0,62}[A-Z0-9])?@[A-Z0-9.-]+\.[A-Z]{2,24}$", re.I)
BAD_EMAIL_PARTS = {
    "example.",
    "yoursite.",
    "mysite.",
    "companyname.",
    "businessname.",
    "yourbusiness.",
    "test@",
    "noreply@",
    "no-reply@",
    "privacy@",
    "abuse@",
    "postmaster@",
}
BAD_TLDS = {"test", "phone", "call", "head", "level", "careers"}


def lead_domains(lead: dict[str, Any]) -> set[str]:
    domains: set[str] = set()
    payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
    for value in [lead.get("company_domain"), lead.get("website"), lead.get("email"), payload.get("root_domain"), payload.get("website")]:
        text = str(value or "")
        if "@" in text and not text.startswith("http"):
            text = text.split("@", 1)[1]
        domain = clean_domain(text)
        if domain:
            domains.add(domain)
    return domains


def lead_has_been_contacted(lead: dict[str, Any]) -> bool:
    summary = lead.get("status_summary") if isinstance(lead.get("status_summary"), dict) else {}
    last_step = summary.get("lastStep") if isinstance(summary.get("lastStep"), dict) else {}
    return bool(lead.get("timestamp_last_contact") or lead.get("timestamp_last_touch") or last_step.get("timestamp_executed"))


def current_leads_by_domain(campaign_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for campaign_id in campaign_ids:
        for lead in list_campaign_leads(campaign_id).values():
            lead["_campaign_id"] = campaign_id
            for domain in lead_domains(lead):
                by_domain.setdefault(domain, []).append(lead)
    return by_domain


def read_priority_rows(path: Path, limit: int = 0, start_row: int = 1, priority_only: bool = True) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            if index < start_row:
                continue
            if priority_only and not row.get("priority_topic"):
                break
            if not row.get("best_contact_email"):
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def valid_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if any(part in email for part in BAD_EMAIL_PARTS):
        return False
    if not EMAIL_RE.fullmatch(email):
        return False
    local, domain = email.rsplit("@", 1)
    tld = domain.rsplit(".", 1)[-1]
    if tld in BAD_TLDS:
        return False
    if local.startswith("20") or local[0].isdigit():
        return False
    return ".." not in local and ".." not in domain and not local.endswith(".")


def lead_payload(row: dict[str, str]) -> dict[str, Any]:
    domain = row["root_domain"].strip().lower()
    topic = row.get("priority_topic") or ""
    return {
        "email": row["best_contact_email"].strip().lower(),
        "first_name": "there",
        "company_name": row.get("site_name") or domain,
        "website": f"https://{domain}",
        "personalization": (
            f"I found {row.get('site_name') or domain} while reviewing Australian publisher sites "
            "that may accept article submissions, sponsored posts, or editorial placements."
        ),
        "custom_variables": {
            "root_domain": domain,
            "site_name": row.get("site_name") or domain,
            "opportunity_type": "guest_post_or_sponsored_article",
            "source_url": row.get("evidence_url") or row.get("source_url") or f"https://{domain}",
            "niche": topic or "Australian publisher",
            "contact_email_type": row.get("best_contact_type") or "",
            "why_relevant": f"the site is categorised as {topic} and has guest post, editorial, advertising, or paid-placement signals",
            "placement_type": "guest post, sponsored article, or editorial placement",
            "pricing_ask": "article submission, sponsored post, or editorial placement pricing and requirements",
            "sender_domain": "ldsearch.com.au",
            "priority_topic": topic,
            "priority_original_position": row.get("priority_original_position") or "",
            "guest_post_fit_tier": row.get("guest_post_fit_tier") or "",
            "guest_post_fit_score": row.get("guest_post_fit_score") or "",
            "acceptance_signals": (row.get("acceptance_signals") or "")[:450],
            "contact_source": row.get("evidence_url") or row.get("source_url") or "",
        },
    }


def write_audit(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "campaign_id",
        "source_campaign_id",
        "root_domain",
        "email",
        "site_name",
        "priority_topic",
        "guest_post_fit_tier",
        "guest_post_fit_score",
        "status",
        "existing_campaign_id",
        "existing_lead_id",
        "instantly_lead_id",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a duplicated guest-post campaign for topic-priority shortlist leads.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-campaign-id", default=ACTIVE_CAMPAIGN_ID)
    parser.add_argument("--name", default="Article Submission Info Request | Topic Priority AU Sites")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-row", type=int, default=1, help="1-based CSV data row, excluding the header.")
    parser.add_argument("--all-rows", action="store_true", help="Do not stop at the priority block; useful for row 70 onward.")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    priority_rows = read_priority_rows(args.input, args.limit, args.start_row, priority_only=not args.all_rows)
    existing = current_leads_by_domain(DEFAULT_CAMPAIGN_IDS)
    leads_to_add: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    for row in priority_rows:
        domain = row["root_domain"].strip().lower()
        if not valid_email(row["best_contact_email"]):
            audit_rows.append(
                {
                    "campaign_id": "",
                    "source_campaign_id": args.source_campaign_id,
                    "root_domain": domain,
                    "email": row["best_contact_email"],
                    "site_name": row.get("site_name") or "",
                    "priority_topic": row.get("priority_topic") or "",
                    "guest_post_fit_tier": row.get("guest_post_fit_tier") or "",
                    "guest_post_fit_score": row.get("guest_post_fit_score") or "",
                    "status": "skipped_invalid_email",
                    "existing_campaign_id": "",
                    "existing_lead_id": "",
                    "instantly_lead_id": "",
                    "notes": "Email failed local validation before Instantly import.",
                }
            )
            continue
        active_existing = [lead for lead in existing.get(domain, []) if not lead_has_been_contacted(lead)]
        contacted_existing = [lead for lead in existing.get(domain, []) if lead_has_been_contacted(lead)]
        if active_existing:
            lead = active_existing[0]
            audit_rows.append(
                {
                    "campaign_id": "",
                    "source_campaign_id": args.source_campaign_id,
                    "root_domain": domain,
                    "email": row["best_contact_email"],
                    "site_name": row.get("site_name") or "",
                    "priority_topic": row.get("priority_topic") or "",
                    "guest_post_fit_tier": row.get("guest_post_fit_tier") or "",
                    "guest_post_fit_score": row.get("guest_post_fit_score") or "",
                    "status": "already_unsent_in_existing_campaign",
                    "existing_campaign_id": lead.get("_campaign_id") or "",
                    "existing_lead_id": lead.get("id") or "",
                    "instantly_lead_id": "",
                    "notes": "Skipped to avoid duplicate active lead.",
                }
            )
            continue
        if contacted_existing:
            lead = contacted_existing[0]
            audit_rows.append(
                {
                    "campaign_id": "",
                    "source_campaign_id": args.source_campaign_id,
                    "root_domain": domain,
                    "email": row["best_contact_email"],
                    "site_name": row.get("site_name") or "",
                    "priority_topic": row.get("priority_topic") or "",
                    "guest_post_fit_tier": row.get("guest_post_fit_tier") or "",
                    "guest_post_fit_score": row.get("guest_post_fit_score") or "",
                    "status": "already_contacted_existing_campaign",
                    "existing_campaign_id": lead.get("_campaign_id") or "",
                    "existing_lead_id": lead.get("id") or "",
                    "instantly_lead_id": "",
                    "notes": "Skipped because this domain has already been contacted.",
                }
            )
            continue
        leads_to_add.append(lead_payload(row))

    print(f"Selected rows: {len(priority_rows)}")
    print(f"New leads to add: {len(leads_to_add)}")
    print(f"Skipped existing: {len(audit_rows)}")
    if args.dry_run:
        for lead in leads_to_add:
            print(f"  {lead['email']} | {lead['custom_variables']['root_domain']} | {lead['custom_variables']['priority_topic']}")
        write_audit(args.output, audit_rows)
        print(f"Dry-run audit: {args.output}")
        return
    if not leads_to_add:
        write_audit(args.output, audit_rows)
        raise SystemExit("No new priority leads to add; not creating campaign.")

    campaign = request(f"/campaigns/{args.source_campaign_id}/duplicate", method="POST", body={"name": args.name})
    campaign_id = str(campaign.get("id") or "")
    if not campaign_id:
        raise RuntimeError(f"Campaign duplicate did not return id: {campaign}")
    request(f"/campaigns/{campaign_id}/pause", method="POST", body={})
    print(f"Created campaign: {campaign_id}")

    duplicated_leads = list_campaign_leads(campaign_id)
    if duplicated_leads:
        raise RuntimeError(f"Duplicated campaign unexpectedly contains {len(duplicated_leads)} leads. It is paused at {campaign_id}.")

    response = request(
        "/leads/add",
        method="POST",
        body={
            "campaign_id": campaign_id,
            "leads": leads_to_add,
            "verify_leads_on_import": False,
            "skip_if_in_workspace": True,
            "skip_if_in_campaign": True,
        },
    )
    created = response.get("created_leads") if isinstance(response.get("created_leads"), list) else []
    ids_by_email = {str(item.get("email") or "").lower(): str(item.get("id") or "") for item in created if isinstance(item, dict)}
    for lead in leads_to_add:
        variables = lead["custom_variables"]
        email = str(lead["email"]).lower()
        audit_rows.append(
            {
                "campaign_id": campaign_id,
                "source_campaign_id": args.source_campaign_id,
                "root_domain": variables["root_domain"],
                "email": email,
                "site_name": variables["site_name"],
                "priority_topic": variables["priority_topic"],
                "guest_post_fit_tier": variables["guest_post_fit_tier"],
                "guest_post_fit_score": variables["guest_post_fit_score"],
                "status": "added_to_new_campaign" if email in ids_by_email else "submitted_to_instantly",
                "existing_campaign_id": "",
                "existing_lead_id": "",
                "instantly_lead_id": ids_by_email.get(email, ""),
                "notes": json_dump_short(response),
            }
        )
    if args.activate:
        request(f"/campaigns/{campaign_id}/activate", method="POST", body={})
        status = "activated"
    else:
        status = "paused"
    write_audit(args.output, audit_rows)
    print(f"Campaign status: {status}")
    print(f"Leads submitted: {len(leads_to_add)}")
    print({k: response.get(k) for k in ["status", "total_sent", "leads_uploaded", "skipped_count", "invalid_email_count", "duplicate_email_count", "remaining_in_plan"]})
    print(f"Audit: {args.output}")


def json_dump_short(value: dict[str, Any]) -> str:
    import json

    return json.dumps({k: value.get(k) for k in ["status", "total_sent", "leads_uploaded", "skipped_count", "invalid_email_count", "duplicate_email_count"]}, sort_keys=True)


if __name__ == "__main__":
    main()
