#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deal_tracker.instantly import ACTIVE_CAMPAIGN_ID, list_campaign_leads, request  # noqa: E402


DEFAULT_OUTPUT = ROOT / "generated" / "imports" / "priority_campaign_move_results.csv"
DEFAULT_IMPORT_AUDIT = ROOT / "generated" / "imports" / "avenuehampers_instantly_import_results.csv"


def lead_has_been_contacted(lead: dict[str, Any]) -> bool:
    summary = lead.get("status_summary") if isinstance(lead.get("status_summary"), dict) else {}
    last_step = summary.get("lastStep") if isinstance(summary.get("lastStep"), dict) else {}
    return bool(
        lead.get("timestamp_last_contact")
        or lead.get("timestamp_last_touch")
        or last_step.get("timestamp_executed")
    )


def priority_leads(campaign_id: str, batch: str) -> list[dict[str, Any]]:
    leads = list_campaign_leads(campaign_id)
    selected: list[dict[str, Any]] = []
    for lead in leads.values():
        payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
        if payload.get("priority_batch") != batch:
            continue
        if lead_has_been_contacted(lead):
            continue
        selected.append(lead)
    selected.sort(key=lambda item: str(item.get("timestamp_created") or ""))
    return selected


def audit_priority_emails(path: Path) -> set[str]:
    if not path.exists():
        return set()
    emails: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") not in {"added_to_campaign", "already_in_campaign"}:
                continue
            sent_state = row.get("sent_state") or ""
            if sent_state == "already_contacted":
                continue
            email = (row.get("lead_email") or "").strip().lower()
            if email:
                emails.add(email)
    return emails


def priority_leads_from_emails(campaign_id: str, emails: set[str]) -> list[dict[str, Any]]:
    leads = list_campaign_leads(campaign_id)
    selected = []
    for email in emails:
        lead = leads.get(email)
        if lead and not lead_has_been_contacted(lead):
            selected.append(lead)
    selected.sort(key=lambda item: str(item.get("timestamp_created") or ""))
    return selected


def write_audit(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "new_campaign_id",
        "source_campaign_id",
        "lead_id",
        "email",
        "company_domain",
        "root_domain",
        "site_name",
        "timestamp_created",
        "status",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def poll_job(job_id: str, timeout_seconds: int = 180) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = request(f"/background-jobs/{job_id}")
        status = str(last.get("status") or "").lower()
        if status in {"completed", "done", "success", "failed", "error"}:
            return last
        time.sleep(3)
    return last


def main() -> None:
    parser = argparse.ArgumentParser(description="Move priority leads into a duplicated Instantly campaign.")
    parser.add_argument("--source-campaign-id", default=ACTIVE_CAMPAIGN_ID)
    parser.add_argument("--priority-batch", default="avenuehampers_csv_next_send")
    parser.add_argument("--new-name", default="Article Submission Info Request | Priority Leads")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--import-audit", type=Path, default=DEFAULT_IMPORT_AUDIT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    selected = priority_leads(args.source_campaign_id, args.priority_batch)
    if not selected:
        audit_emails = audit_priority_emails(args.import_audit)
        selected = priority_leads_from_emails(args.source_campaign_id, audit_emails)
    print(f"Priority unsent leads: {len(selected)}")
    for lead in selected:
        print(f"  {lead.get('email')} | {lead.get('company_domain')} | {lead.get('id')}")

    if args.dry_run:
        return
    if not selected:
        raise SystemExit("No unsent priority leads found; not creating a campaign.")

    new_campaign = request(
        f"/campaigns/{args.source_campaign_id}/duplicate",
        method="POST",
        body={"name": args.new_name},
    )
    new_campaign_id = str(new_campaign.get("id") or "")
    if not new_campaign_id:
        raise RuntimeError(f"Duplicate campaign did not return an id: {new_campaign}")
    print(f"New campaign: {new_campaign_id}")

    # The duplicate endpoint can inherit campaign status, so pause immediately until lead contents are checked.
    request(f"/campaigns/{new_campaign_id}/pause", method="POST", body={})

    duplicated_leads = list_campaign_leads(new_campaign_id)
    if duplicated_leads:
        request(f"/campaigns/{new_campaign_id}/pause", method="POST", body={})
        raise RuntimeError(
            f"Duplicated campaign unexpectedly contains {len(duplicated_leads)} leads. "
            f"It is paused at {new_campaign_id}; inspect before activating."
        )

    move_body = {
        "campaign": args.source_campaign_id,
        "ids": [str(lead["id"]) for lead in selected],
        "to_campaign_id": new_campaign_id,
        "copy_leads": False,
        "ignore_resource_filter_clauses": True,
        "check_duplicates": True,
        "check_duplicates_in_campaigns": False,
        "skip_leads_in_verification": False,
        "reset_interest_status": True,
    }
    move_job = request("/leads/move", method="POST", body=move_body)
    job_id = str(move_job.get("id") or "")
    print(f"Move job: {job_id or move_job}")
    if job_id:
        final_job = poll_job(job_id)
        print(f"Move job status: {final_job.get('status')} progress={final_job.get('progress')}")

    moved = list_campaign_leads(new_campaign_id)
    moved_emails = {str(lead.get("email") or "").lower() for lead in moved.values()}
    expected_emails = {str(lead.get("email") or "").lower() for lead in selected}
    missing = expected_emails - moved_emails
    if missing:
        raise RuntimeError(f"Not all priority leads arrived in the new campaign: {sorted(missing)}")

    audit_rows: list[dict[str, Any]] = []
    for lead in moved.values():
        payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
        audit_rows.append(
            {
                "new_campaign_id": new_campaign_id,
                "source_campaign_id": args.source_campaign_id,
                "lead_id": lead.get("id") or "",
                "email": lead.get("email") or "",
                "company_domain": lead.get("company_domain") or "",
                "root_domain": payload.get("root_domain") or "",
                "site_name": payload.get("site_name") or lead.get("company_name") or "",
                "timestamp_created": lead.get("timestamp_created") or "",
                "status": lead.get("status") or "",
            }
        )
    write_audit(args.output, audit_rows)

    request(f"/campaigns/{new_campaign_id}/activate", method="POST", body={})
    print(f"Activated campaign: {new_campaign_id}")
    print(f"Moved leads: {len(moved)}")
    print(f"Audit: {args.output}")


if __name__ == "__main__":
    main()
