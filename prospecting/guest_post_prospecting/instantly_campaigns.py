from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .apify_google import load_env_value
from .db import connect, init_db
from .paths import GENERATED_DIR, REVIEW_DIR, ensure_project_dirs
from .utils import clean_domain, now_iso, safe_slug


INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"
MASTER_PAID_LINK_SHEET = REVIEW_DIR / "master_paid_link_review_sheet.csv"
CAMPAIGN_DRAFT_DIR = GENERATED_DIR / "campaign_drafts"
INSTANTLY_UPLOAD_DIR = GENERATED_DIR / "instantly_uploads"
DEFAULT_SUBJECT = "Article Submission Info Request"
DEFAULT_SENDER = "laurence.d@ldsearch.com.au"


@dataclass(frozen=True)
class CampaignBuildConfig:
    input: Path = MASTER_PAID_LINK_SHEET
    campaign_name: str = "Article Submission Info Request | AU Sites"
    subject: str = DEFAULT_SUBJECT
    sender: str = DEFAULT_SENDER
    execute: bool = False
    include_out_of_scope: bool = True
    include_role_pattern_guesses: bool = True
    max_leads: int = 0
    daily_max_leads: int = 0
    email_gap_minutes: int = 45
    random_wait_max_minutes: int = 15
    batch_size: int = 1000


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def clean_text(value: str, limit: int = 500) -> str:
    return " ".join((value or "").split())[:limit]


def site_name_for_row(row: dict[str, str]) -> str:
    site_name = clean_text(row.get("site_name", ""), 120)
    if site_name:
        return site_name
    domain = clean_domain(row.get("root_domain", ""))
    return domain or "your site"


def lead_priority(row: dict[str, str]) -> tuple[int, int, int, str]:
    likelihood_rank = {"Very Likely": 0, "Likely": 1, "Possible": 2, "Weak": 3, "Out Of Scope": 4}
    method_rank = {"original_paid_link_review": 0, "page_scrape": 1, "role_pattern_unverified": 2}
    score = int(row.get("paid_link_score") or 0)
    return (
        likelihood_rank.get(row.get("paid_link_likelihood", ""), 9),
        method_rank.get(row.get("best_contact_method", ""), 9),
        -score,
        row.get("root_domain", ""),
    )


def selected_contact_rows(rows: list[dict[str, str]], config: CampaignBuildConfig) -> list[dict[str, str]]:
    selected = []
    for row in rows:
        email = (row.get("best_contact_email") or "").strip().lower()
        if not email:
            continue
        if not config.include_out_of_scope and row.get("paid_link_likelihood") == "Out Of Scope":
            continue
        if not config.include_role_pattern_guesses and row.get("best_contact_is_role_guess") == "yes":
            continue
        selected.append({**row, "best_contact_email": email})
    selected.sort(key=lead_priority)
    if config.max_leads:
        selected = selected[: config.max_leads]
    return selected


def dedupe_rows_by_email(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    by_email: dict[str, dict[str, str]] = {}
    duplicate_rows: list[dict[str, str]] = []
    for row in rows:
        email = row["best_contact_email"]
        if email not in by_email:
            by_email[email] = row
            continue
        current = by_email[email]
        if lead_priority(row) < lead_priority(current):
            duplicate_rows.append(current)
            by_email[email] = row
        else:
            duplicate_rows.append(row)

    grouped_domains: dict[str, list[str]] = {}
    grouped_source_urls: dict[str, list[str]] = {}
    for row in rows:
        email = row["best_contact_email"]
        grouped_domains.setdefault(email, [])
        grouped_source_urls.setdefault(email, [])
        domain = clean_domain(row.get("root_domain", ""))
        source_url = row.get("source_url", "")
        if domain and domain not in grouped_domains[email]:
            grouped_domains[email].append(domain)
        if source_url and source_url not in grouped_source_urls[email]:
            grouped_source_urls[email].append(source_url)

    deduped = []
    for email, row in by_email.items():
        deduped.append(
            {
                **row,
                "additional_domains_for_email": "; ".join(domain for domain in grouped_domains[email] if domain != clean_domain(row.get("root_domain", ""))),
                "all_source_urls_for_email": "; ".join(grouped_source_urls[email]),
                "domain_count_for_email": str(len(grouped_domains[email])),
            }
        )
    deduped.sort(key=lead_priority)
    return deduped, duplicate_rows


def sequence_steps(subject: str) -> list[dict[str, Any]]:
    bodies = [
        """Hi {{firstName}},

I'm Laurence Deer. I manage SEO and content promotion for a large portfolio of Australian ecommerce stores.

I wanted to ask whether {{site_name}} accepts article submissions, sponsored articles, guest posts, advertorials, or similar editorial placements.

If so, could you let me know whether there are any fees, topic guidelines, link requirements, or other conditions?

Regards,
Laurence
LD Search
ldsearch.com.au

If this is not relevant, just reply "no" and I won't follow up.""",
        """Hi {{firstName}},

Just following up on this.

I'm looking for the right process for article submissions or sponsored/editorial placements on {{site_name}}.

Is there a rate card, media kit, or contributor guideline page I should look at?

Regards,
Laurence""",
        """Hi {{firstName}},

Quick nudge from me.

We work with Australian ecommerce brands and often need relevant sites for useful content placements.

Do you accept paid article submissions or sponsored content, and what are the usual fees or requirements?

Regards,
Laurence""",
        """Hi {{firstName}},

Is there someone else who handles article submissions, advertising, or sponsored content enquiries for {{site_name}}?

If yes, could you point me in the right direction?

Regards,
Laurence""",
        """Hi {{firstName}},

I'm still trying to confirm whether {{site_name}} offers any paid editorial, guest post, sponsored article, or content placement options.

Even a short reply with "yes, send details" or "no, we don't offer this" would be helpful.

Regards,
Laurence""",
        """Hi {{firstName}},

Last follow-up from me.

If {{site_name}} does accept paid article submissions or content placements, could you send through the fees and requirements?

If not, no worries at all.

Regards,
Laurence""",
    ]
    delays = [0, 3, 5, 7, 10, 14]
    steps = []
    for index, body in enumerate(bodies):
        steps.append(
            {
                "type": "email",
                "delay": delays[index],
                "delay_unit": "days",
                "pre_delay": delays[index],
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": subject,
                        "body": body,
                        "v_disabled": False,
                    }
                ],
            }
        )
    return steps


def campaign_payload(config: CampaignBuildConfig) -> dict[str, Any]:
    today = date.today().isoformat()
    return {
        "name": config.campaign_name,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "AU business hours",
                    "timing": {"from": "09:00", "to": "17:00"},
                    "days": {"0": True, "1": True, "2": True, "3": True, "4": True, "5": False, "6": False},
                    "timezone": "Australia/Melbourne",
                }
            ],
            "start_date": today,
        },
        "sequences": [{"steps": sequence_steps(config.subject)}],
        "email_list": [config.sender],
        "email_gap": config.email_gap_minutes,
        "random_wait_max": config.random_wait_max_minutes,
        "text_only": True,
        "first_email_text_only": True,
        "daily_limit": 20,
        "daily_max_leads": config.daily_max_leads,
        "stop_on_reply": True,
        "stop_on_auto_reply": True,
        "stop_for_company": True,
        "link_tracking": False,
        "open_tracking": False,
        "insert_unsubscribe_header": True,
        "allow_risky_contacts": True,
        "disable_bounce_protect": False,
    }


def lead_payload(row: dict[str, str]) -> dict[str, Any]:
    root_domain = clean_domain(row.get("root_domain", ""))
    site_name = site_name_for_row(row)
    source_url = row.get("source_url") or f"https://{root_domain}"
    return {
        "email": row.get("best_contact_email", ""),
        "first_name": "there",
        "company_name": site_name,
        "website": f"https://{root_domain}" if root_domain else source_url,
        "personalization": f"I found {site_name} while reviewing Australian sites that may accept article submissions or sponsored/editorial placements.",
        "custom_variables": {
            "root_domain": root_domain,
            "site_name": site_name,
            "source_url": source_url,
            "matched_phrase": clean_text(row.get("matched_phrase", ""), 120),
            "opportunity_type": clean_text(row.get("opportunity_type", ""), 80),
            "niche": clean_text(row.get("niche", ""), 80),
            "paid_link_likelihood": clean_text(row.get("paid_link_likelihood", ""), 40),
            "paid_link_score": clean_text(row.get("paid_link_score", ""), 10),
            "transaction_type": clean_text(row.get("transaction_type", ""), 80),
            "contact_email_type": clean_text(row.get("best_contact_email_type", ""), 40),
            "contact_source_method": clean_text(row.get("best_contact_method", ""), 80),
            "contact_source": clean_text(row.get("best_contact_source", ""), 250),
            "is_role_pattern_guess": clean_text(row.get("best_contact_is_role_guess", ""), 10),
            "why_relevant": clean_text(row.get("why_relevant", ""), 240),
            "seller_style_flags": clean_text(row.get("seller_style_flags", ""), 180),
            "additional_domains_for_email": clean_text(row.get("additional_domains_for_email", ""), 300),
            "domain_count_for_email": clean_text(row.get("domain_count_for_email", ""), 10),
        },
    }


def upload_batch_payload(campaign_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "campaign_id": campaign_id,
        "leads": leads,
        "verify_leads_on_import": False,
        "skip_if_in_workspace": True,
        "skip_if_in_campaign": True,
    }


def instantly_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "GuestPostOutreach/1.0 (+https://ldsearch.com.au)",
    }


def instantly_request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None, token: str) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{INSTANTLY_BASE_URL}{path}",
        data=data,
        method=method,
        headers=instantly_headers(token),
    )
    try:
        with urlopen(request, timeout=90) as response:
            payload = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except HTTPError as exc:
        error_payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instantly {method} {path} failed with HTTP {exc.code}: {error_payload[:1000]}") from exc
    return json.loads(payload or "{}")


def batched(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), max(1, size))]


def persist_campaign_draft(draft_id: str, name: str, status: str, payload: dict[str, Any], upload_summary: dict[str, Any]) -> None:
    init_db()
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            insert into guest_post_campaign_drafts (id, name, status, campaign_group, sequence_json, payload_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
                name=excluded.name,
                status=excluded.status,
                campaign_group=excluded.campaign_group,
                sequence_json=excluded.sequence_json,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                draft_id,
                name,
                status,
                "Guest Post Outreach",
                json.dumps(payload.get("sequences", []), indent=2),
                json.dumps({"campaign_payload": payload, "upload_summary": upload_summary}, indent=2, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()


def build_or_create_campaign(config: CampaignBuildConfig) -> dict[str, Any]:
    ensure_project_dirs()
    if not config.input.exists():
        raise FileNotFoundError(f"Input CSV not found: {config.input}")

    all_rows = read_csv_rows(config.input)
    contact_rows = selected_contact_rows(all_rows, config)
    deduped_rows, duplicate_rows = dedupe_rows_by_email(contact_rows)
    leads = [lead_payload(row) for row in deduped_rows]
    payload = campaign_payload(config)

    slug = safe_slug(config.campaign_name.lower())[:80]
    timestamp = str(int(time.time()))
    draft_id = f"{slug}_{timestamp}"
    campaign_json_path = CAMPAIGN_DRAFT_DIR / f"{draft_id}_campaign_payload.json"
    upload_json_path = INSTANTLY_UPLOAD_DIR / f"{draft_id}_lead_upload_payload.json"
    leads_csv_path = INSTANTLY_UPLOAD_DIR / f"{draft_id}_leads.csv"
    duplicates_csv_path = INSTANTLY_UPLOAD_DIR / f"{draft_id}_duplicate_email_domains.csv"
    summary_path = CAMPAIGN_DRAFT_DIR / f"{draft_id}_summary.json"

    campaign_json_path.parent.mkdir(parents=True, exist_ok=True)
    upload_json_path.parent.mkdir(parents=True, exist_ok=True)
    campaign_json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    upload_json_path.write_text(json.dumps({"campaign_id": "FILL_AFTER_CREATE", "leads": leads[:1000]}, indent=2, sort_keys=True), encoding="utf-8")

    lead_rows = []
    for row, lead in zip(deduped_rows, leads):
        custom = lead["custom_variables"]
        lead_rows.append(
            {
                "email": lead["email"],
                "first_name": lead["first_name"],
                "company_name": lead["company_name"],
                "website": lead["website"],
                "root_domain": custom["root_domain"],
                "site_name": custom["site_name"],
                "paid_link_likelihood": custom["paid_link_likelihood"],
                "paid_link_score": custom["paid_link_score"],
                "transaction_type": custom["transaction_type"],
                "contact_email_type": custom["contact_email_type"],
                "contact_source_method": custom["contact_source_method"],
                "is_role_pattern_guess": custom["is_role_pattern_guess"],
                "source_url": custom["source_url"],
                "additional_domains_for_email": custom["additional_domains_for_email"],
                "domain_count_for_email": custom["domain_count_for_email"],
            }
        )
    write_csv(leads_csv_path, lead_rows, list(lead_rows[0].keys()) if lead_rows else ["email"])
    duplicate_fields = sorted({key for row in duplicate_rows for key in row.keys()}) if duplicate_rows else ["root_domain", "best_contact_email"]
    write_csv(duplicates_csv_path, duplicate_rows, duplicate_fields)

    api_result: dict[str, Any] = {}
    campaign_id = ""
    upload_results: list[dict[str, Any]] = []
    if config.execute:
        token = os.environ.get("INSTANTLY_API_KEY") or load_env_value("INSTANTLY_API_KEY")
        if not token:
            raise RuntimeError("INSTANTLY_API_KEY is missing from this repo's .env")
        campaign = instantly_request("/campaigns", method="POST", body=payload, token=token)
        campaign_id = str(campaign.get("id") or "")
        if not campaign_id:
            raise RuntimeError(f"Instantly campaign create did not return an id: {campaign}")
        pause_result = instantly_request(f"/campaigns/{campaign_id}/pause", method="POST", body={}, token=token)
        for batch in batched(leads, config.batch_size):
            upload_results.append(instantly_request("/leads/add", method="POST", body=upload_batch_payload(campaign_id, batch), token=token))
        api_result = {"campaign": campaign, "pause_result": pause_result, "upload_results": upload_results}

    summary = {
        "campaign_name": config.campaign_name,
        "subject": config.subject,
        "sender": config.sender,
        "execute": config.execute,
        "campaign_id": campaign_id,
        "input_rows": len(all_rows),
        "rows_with_contact_selected": len(contact_rows),
        "deduped_leads": len(deduped_rows),
        "duplicate_email_rows": len(duplicate_rows),
        "sequence_steps": len(payload["sequences"][0]["steps"]),
        "followup_steps": len(payload["sequences"][0]["steps"]) - 1,
        "daily_max_leads": config.daily_max_leads,
        "campaign_payload": str(campaign_json_path),
        "lead_upload_payload_sample": str(upload_json_path),
        "leads_csv": str(leads_csv_path),
        "duplicate_email_rows_csv": str(duplicates_csv_path),
        "api_result": api_result,
        "generated_at": now_iso(),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    persist_campaign_draft(draft_id, config.campaign_name, "created" if campaign_id else "draft", payload, summary)
    summary["summary"] = str(summary_path)
    return summary
