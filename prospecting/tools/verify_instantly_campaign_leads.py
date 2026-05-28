#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.apify_google import load_env_value
from guest_post_prospecting.paths import GENERATED_DIR


BASE_URL = "https://api.instantly.ai/api/v2"
DEFAULT_CAMPAIGN_ID = "945b0afd-7c53-492d-9afe-4c44f57173ad"
INSTANTLY_UPLOAD_DIR = GENERATED_DIR / "instantly_uploads"


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "GuestPostOutreach/1.0 (+https://ldsearch.com.au)",
    }


def api_request(path: str, *, method: str = "GET", body: dict | None = None, token: str) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(BASE_URL + path, data=data, method=method, headers=headers(token))
    try:
        with urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8") or "{}")
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} HTTP {exc.code}: {payload[:1000]}") from exc


def list_campaign_leads(campaign_id: str, token: str) -> list[dict]:
    leads: list[dict] = []
    starting_after = ""
    while True:
        body = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        data = api_request("/leads/list", method="POST", body=body, token=token)
        batch = data.get("items") or []
        leads.extend(item for item in batch if isinstance(item, dict))
        starting_after = data.get("next_starting_after") or ""
        if not starting_after or not batch:
            return leads


def row_from_result(lead: dict, result: dict, *, action: str, error: str = "") -> dict[str, str]:
    payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
    return {
        "lead_id": str(lead.get("id") or ""),
        "email": str(lead.get("email") or result.get("email") or ""),
        "company_name": str(lead.get("company_name") or ""),
        "website": str(lead.get("website") or ""),
        "root_domain": str(payload.get("root_domain") or lead.get("company_domain") or ""),
        "action": action,
        "verification_status": str(result.get("verification_status") or ""),
        "status": str(result.get("status") or ""),
        "catch_all": str(result.get("catch_all") or ""),
        "credits": str(result.get("credits") or ""),
        "credits_used": str(result.get("credits_used") or ""),
        "error": error,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def read_existing(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row.get("email", "").lower(): row for row in csv.DictReader(handle) if row.get("email")}


def append_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "lead_id",
        "email",
        "company_name",
        "website",
        "root_domain",
        "action",
        "verification_status",
        "status",
        "catch_all",
        "credits",
        "credits_used",
        "error",
        "checked_at",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def verify_one(lead: dict, token: str) -> dict[str, str]:
    email = str(lead.get("email") or "").strip().lower()
    try:
        result = api_request("/email-verification", method="POST", body={"email": email}, token=token)
        return row_from_result(lead, result, action="submitted")
    except RuntimeError as exc:
        return row_from_result(lead, {}, action="error", error=str(exc))


def poll_one(row: dict[str, str], token: str) -> dict[str, str]:
    email = row["email"].strip().lower()
    lead = {"id": row.get("lead_id", ""), "email": email, "company_name": row.get("company_name", ""), "website": row.get("website", ""), "payload": {"root_domain": row.get("root_domain", "")}}
    try:
        result = api_request(f"/email-verification/{quote(email, safe='')}", token=token)
        return row_from_result(lead, result, action="polled")
    except RuntimeError as exc:
        return row_from_result(lead, {}, action="poll_error", error=str(exc))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Instantly campaign leads with Instantly email verification credits.")
    parser.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
    parser.add_argument("--output", type=Path, default=INSTANTLY_UPLOAD_DIR / "instantly_email_verification_results.csv")
    parser.add_argument("--summary", type=Path, default=INSTANTLY_UPLOAD_DIR / "instantly_email_verification_summary.json")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-emails", type=int, default=0)
    parser.add_argument("--poll-pending", action="store_true")
    parser.add_argument("--poll-rounds", type=int, default=3)
    parser.add_argument("--poll-sleep-seconds", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = os.environ.get("INSTANTLY_API_KEY") or load_env_value("INSTANTLY_API_KEY")
    if not token:
        raise RuntimeError("INSTANTLY_API_KEY is missing from the environment or this repo's .env")

    leads = list_campaign_leads(args.campaign_id, token)
    existing = read_existing(args.output)
    pending_leads = [lead for lead in leads if str(lead.get("email") or "").lower() not in existing]
    if args.max_emails:
        pending_leads = pending_leads[: args.max_emails]

    started = time.time()
    submitted_rows: list[dict[str, str]] = []
    stop_after_credit_error = False
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(verify_one, lead, token): lead for lead in pending_leads}
        buffer: list[dict[str, str]] = []
        for completed, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            buffer.append(row)
            submitted_rows.append(row)
            if "HTTP 402" in row.get("error", ""):
                stop_after_credit_error = True
            if len(buffer) >= 25 or completed == len(futures):
                append_rows(args.output, buffer)
                buffer = []
                write_json(
                    args.summary,
                    {
                        "campaign_id": args.campaign_id,
                        "campaign_leads_seen": len(leads),
                        "already_had_results": len(existing),
                        "submitted_this_run": len(submitted_rows),
                        "planned_this_run": len(pending_leads),
                        "stop_after_credit_error": stop_after_credit_error,
                        "elapsed_seconds": round(time.time() - started, 2),
                        "output": str(args.output),
                    },
                )
            if stop_after_credit_error:
                break

    if args.poll_pending:
        for _round in range(args.poll_rounds):
            current = read_existing(args.output)
            pending = [row for row in current.values() if row.get("verification_status") == "pending"]
            if not pending:
                break
            time.sleep(args.poll_sleep_seconds)
            polled_rows: list[dict[str, str]] = []
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
                futures = [executor.submit(poll_one, row, token) for row in pending]
                for future in as_completed(futures):
                    polled_rows.append(future.result())
            append_rows(args.output, polled_rows)

    all_results = list(read_existing(args.output).values())
    counts: dict[str, int] = {}
    for row in all_results:
        key = row.get("verification_status") or row.get("action") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    credits_left = ""
    for row in reversed(all_results):
        if row.get("credits"):
            credits_left = row["credits"]
            break
    summary = {
        "campaign_id": args.campaign_id,
        "campaign_leads_seen": len(leads),
        "results_rows_latest_by_email": len(all_results),
        "submitted_this_run": len(submitted_rows),
        "counts_by_latest_status": counts,
        "credits_left_last_seen": credits_left,
        "output": str(args.output),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    write_json(args.summary, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
