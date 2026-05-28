from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .apify_google import load_env_value
from .db import connect, init_db
from .instantly_campaigns import INSTANTLY_BASE_URL, instantly_headers
from .utils import clean_domain, now_iso


ACTIVE_CAMPAIGN_ID = "7bc7d5e3-924d-463b-b875-8e8301be264b"
OLD_CAMPAIGN_ID = "945b0afd-7c53-492d-9afe-4c44f57173ad"
DEFAULT_CAMPAIGN_IDS = [ACTIVE_CAMPAIGN_ID, OLD_CAMPAIGN_ID]
INBOUND_UE_TYPE = 2

DEAL_STATUSES = {"needs_review", "confirmed", "rejected", "archived"}
PLACEMENT_TYPES = {"guest_post", "sponsored_post", "advertorial", "niche_edit", "media_package", "other"}


@dataclass(frozen=True)
class ReplyExtraction:
    classification: str
    deal_status: str
    placement_type: str
    price_amount: float | None
    price_currency: str
    price_notes: str
    writing_requirements: str
    link_requirements: str
    turnaround_time: str
    publisher_url: str
    reply_summary: str
    create_deal: bool

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)


def token() -> str:
    value = os.environ.get("INSTANTLY_API_KEY") or load_env_value("INSTANTLY_API_KEY")
    if not value:
        raise RuntimeError("INSTANTLY_API_KEY is missing from the environment or this repo's .env")
    return value


def instantly_request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    qs = f"?{urlencode(params)}" if params else ""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{INSTANTLY_BASE_URL}{path}{qs}",
        data=data,
        method=method,
        headers=instantly_headers(token()),
    )
    try:
        with urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8") or "{}")
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instantly {method} {path} failed with HTTP {exc.code}: {payload[:1000]}") from exc


def list_campaign_emails(campaign_id: str) -> list[dict[str, Any]]:
    emails: list[dict[str, Any]] = []
    starting_after = ""
    while True:
        params: dict[str, Any] = {"campaign_id": campaign_id, "limit": 100, "sort_order": "desc"}
        if starting_after:
            params["starting_after"] = starting_after
        payload = instantly_request("/emails", params=params)
        batch = payload.get("items") or []
        emails.extend(item for item in batch if isinstance(item, dict))
        starting_after = payload.get("next_starting_after") or ""
        if not starting_after or not batch:
            return emails


def list_campaign_leads(campaign_id: str) -> dict[str, dict[str, Any]]:
    leads: dict[str, dict[str, Any]] = {}
    starting_after = ""
    while True:
        body: dict[str, Any] = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        payload = instantly_request("/leads/list", method="POST", body=body)
        batch = payload.get("items") or []
        for item in batch:
            if isinstance(item, dict) and item.get("email"):
                leads[str(item["email"]).lower()] = item
        starting_after = payload.get("next_starting_after") or ""
        if not starting_after or not batch:
            return leads


def inbound_replies(campaign_id: str) -> list[dict[str, Any]]:
    return [email for email in list_campaign_emails(campaign_id) if email.get("ue_type") == INBOUND_UE_TYPE]


def email_body_text(email: dict[str, Any]) -> str:
    body = email.get("body")
    if isinstance(body, dict):
        text = str(body.get("text") or "")
        if text:
            return text.strip()
        html = str(body.get("html") or "")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return str(body or "").strip()


def first_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>)\"']+", text)
    return match.group(0).rstrip(".,") if match else ""


def first_price(text: str) -> tuple[float | None, str]:
    patterns = [
        r"(?:AUD|AU\$|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:AUD|Australian dollars)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            raw = match.group(1).replace(",", "")
            try:
                return float(raw), match.group(0)
            except ValueError:
                return None, match.group(0)
    return None, ""


def top_reply_text(body_text: str) -> str:
    text = body_text or ""
    markers = [
        "\n-----Original Message-----",
        "\nFrom:",
        "\nOn ",
        "\nSent:",
    ]
    cut = len(text)
    for marker in markers:
        index = text.find(marker)
        if index > 0:
            cut = min(cut, index)
    return text[:cut].strip() or text.strip()


def classify_placement(text: str) -> str:
    lowered = text.lower()
    if "niche edit" in lowered or "link insertion" in lowered:
        return "niche_edit"
    if "advertorial" in lowered:
        return "advertorial"
    if "media kit" in lowered or "rate card" in lowered or "advertising package" in lowered:
        return "media_package"
    if "sponsored" in lowered:
        return "sponsored_post"
    if "guest post" in lowered or "article submission" in lowered or "contributor" in lowered:
        return "guest_post"
    return "other"


def sentence_with_any(text: str, needles: list[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for sentence in sentences:
        lowered = sentence.lower()
        if any(needle in lowered for needle in needles):
            return sentence.strip()[:700]
    return ""


def classify_reply(body_text: str) -> ReplyExtraction:
    text = re.sub(r"\s+", " ", top_reply_text(body_text)).strip()
    lowered = text.lower()
    amount, price_note = first_price(text)
    placement_type = classify_placement(text)
    publisher_url = first_url(text)

    auto_markers = ["out of office", "auto-reply", "autoreply", "automatic reply", "only applicants with successful submissions", "do not reply"]
    reject_markers = [
        "do not accept",
        "don't accept",
        "not accepting",
        "no sponsored",
        "no paid",
        "not offer",
        "we don't offer",
        "unable to assist",
        "isn't one for us",
        "is not one for us",
        "not one for us",
    ]
    money_markers = ["rate card", "media kit", "pricing", "price", "cost", "fee", "package", "sponsored", "advertorial", "paid placement", "guest post"]
    process_markers = ["send through", "guidelines", "requirements", "word count", "dofollow", "turnaround", "invoice", "payment", "editorial"]

    if any(marker in lowered for marker in reject_markers):
        classification = "rejected"
        deal_status = "rejected"
        create_deal = False
        summary = "Reply indicates they do not accept the requested placement."
    elif any(marker in lowered for marker in auto_markers):
        classification = "auto_reply"
        deal_status = "archived"
        create_deal = False
        summary = "Auto-reply or submission acknowledgement; no deal terms confirmed."
    elif amount is not None or any(marker in lowered for marker in money_markers):
        classification = "deal_terms"
        deal_status = "confirmed" if amount is not None else "needs_review"
        create_deal = True
        summary = "Reply appears to include pricing, a rate card/media kit, or paid placement terms."
    elif any(marker in lowered for marker in process_markers):
        classification = "needs_review"
        deal_status = "needs_review"
        create_deal = True
        summary = "Reply appears to provide a possible paid placement process but needs review."
    else:
        classification = "ignore"
        deal_status = "archived"
        create_deal = False
        summary = "No clear paid placement terms found."

    writing = sentence_with_any(text, ["word count", "article", "content", "guidelines", "submission", "editorial"])
    links = sentence_with_any(text, ["link", "dofollow", "nofollow", "anchor"])
    turnaround = sentence_with_any(text, ["turnaround", "business day", "week", "publish"])
    if amount is not None and not price_note:
        price_note = f"Detected price: {amount:g}"

    return ReplyExtraction(
        classification=classification,
        deal_status=deal_status,
        placement_type=placement_type,
        price_amount=amount,
        price_currency="AUD",
        price_notes=price_note,
        writing_requirements=writing,
        link_requirements=links,
        turnaround_time=turnaround,
        publisher_url=publisher_url,
        reply_summary=summary,
        create_deal=create_deal,
    )


def lead_context(lead: dict[str, Any] | None, email: dict[str, Any]) -> dict[str, str]:
    lead = lead or {}
    payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
    lead_email = str(email.get("lead") or lead.get("email") or "").lower()
    website = str(lead.get("website") or payload.get("website") or "")
    root_domain = str(payload.get("root_domain") or clean_domain(website) or clean_domain(lead_email.rsplit("@", 1)[-1] if "@" in lead_email else ""))
    site_name = str(payload.get("site_name") or payload.get("companyName") or lead.get("company_name") or root_domain)
    return {
        "root_domain": root_domain,
        "site_name": site_name,
        "contact_email": lead_email,
        "instantly_lead_id": str(lead.get("id") or email.get("lead_id") or ""),
        "publisher_url": website,
    }


def save_reply_and_maybe_deal(campaign_id: str, email: dict[str, Any], lead: dict[str, Any] | None = None) -> bool:
    init_db()
    body_text = email_body_text(email)
    extraction = classify_reply(body_text)
    context = lead_context(lead, email)
    processed_at = now_iso()
    email_id = str(email.get("id") or "")
    thread_id = str(email.get("thread_id") or "")
    from_email = str(email.get("from_address_email") or "")
    to_email = str(email.get("to_address_email_list") or "")
    subject = str(email.get("subject") or "")
    received_at = str(email.get("timestamp_email") or email.get("timestamp_created") or "")

    with connect() as connection:
        connection.execute(
            """
            insert into instantly_reply_sync (
                campaign_id, email_id, lead_email, thread_id, subject, from_email, to_email,
                body_text, received_at, classification, extracted_json, processed_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(email_id) do update set
                classification=excluded.classification,
                extracted_json=excluded.extracted_json,
                processed_at=excluded.processed_at
            """,
            (
                campaign_id,
                email_id,
                context["contact_email"],
                thread_id,
                subject,
                from_email,
                to_email,
                body_text,
                received_at,
                extraction.classification,
                extraction.to_json(),
                processed_at,
            ),
        )
        created_deal = False
        if extraction.create_deal:
            existing = connection.execute(
                "select id, additional_notes from link_deals where instantly_thread_id = ? and contact_email = ?",
                (thread_id, context["contact_email"]),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    update link_deals set
                        root_domain=?, site_name=?, source_campaign_id=?, instantly_lead_id=?,
                        deal_status=?, placement_type=?, price_amount=coalesce(?, price_amount),
                        price_currency=?, price_notes=coalesce(nullif(?, ''), price_notes),
                        publisher_url=coalesce(nullif(?, ''), publisher_url),
                        writing_requirements=coalesce(nullif(?, ''), writing_requirements),
                        link_requirements=coalesce(nullif(?, ''), link_requirements),
                        turnaround_time=coalesce(nullif(?, ''), turnaround_time),
                        reply_summary=?, reply_evidence_text=?, updated_at=?
                    where id=?
                    """,
                    (
                        context["root_domain"],
                        context["site_name"],
                        campaign_id,
                        context["instantly_lead_id"],
                        extraction.deal_status,
                        extraction.placement_type,
                        extraction.price_amount,
                        extraction.price_currency,
                        extraction.price_notes,
                        extraction.publisher_url or context["publisher_url"],
                        extraction.writing_requirements,
                        extraction.link_requirements,
                        extraction.turnaround_time,
                        extraction.reply_summary,
                        body_text,
                        processed_at,
                        existing["id"],
                    ),
                )
            else:
                connection.execute(
                    """
                    insert into link_deals (
                        root_domain, site_name, contact_email, source_campaign_id, instantly_lead_id,
                        instantly_thread_id, deal_status, placement_type, price_amount, price_currency,
                        price_notes, domain_trust_source, publisher_url, writing_requirements,
                        link_requirements, turnaround_time, reply_summary, reply_evidence_text,
                        created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        context["root_domain"],
                        context["site_name"],
                        context["contact_email"],
                        campaign_id,
                        context["instantly_lead_id"],
                        thread_id,
                        extraction.deal_status,
                        extraction.placement_type,
                        extraction.price_amount,
                        extraction.price_currency,
                        extraction.price_notes,
                        "manual_seranking_later",
                        extraction.publisher_url or context["publisher_url"],
                        extraction.writing_requirements,
                        extraction.link_requirements,
                        extraction.turnaround_time,
                        extraction.reply_summary,
                        body_text,
                        processed_at,
                        processed_at,
                    ),
                )
                created_deal = True
        connection.commit()
    return created_deal


def sync_replies(campaign_ids: list[str] | None = None) -> dict[str, Any]:
    campaign_ids = campaign_ids or DEFAULT_CAMPAIGN_IDS
    summary = {"campaigns": {}, "inbound_replies": 0, "new_or_updated_deals": 0}
    for campaign_id in campaign_ids:
        leads = list_campaign_leads(campaign_id)
        replies = inbound_replies(campaign_id)
        created = 0
        for reply in replies:
            lead_email = str(reply.get("lead") or "").lower()
            if save_reply_and_maybe_deal(campaign_id, reply, leads.get(lead_email)):
                created += 1
        summary["campaigns"][campaign_id] = {"inbound_replies": len(replies), "new_deals": created}
        summary["inbound_replies"] += len(replies)
        summary["new_or_updated_deals"] += created
    return summary


def row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def list_deals(filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    init_db()
    filters = filters or {}
    clauses: list[str] = []
    values: list[Any] = []
    if filters.get("status"):
        clauses.append("deal_status = ?")
        values.append(filters["status"])
    if filters.get("placement_type"):
        clauses.append("placement_type = ?")
        values.append(filters["placement_type"])
    if filters.get("price_present") == "yes":
        clauses.append("price_amount is not null")
    if filters.get("price_present") == "no":
        clauses.append("price_amount is null")
    if filters.get("domain"):
        clauses.append("(root_domain like ? or site_name like ? or contact_email like ?)")
        needle = f"%{filters['domain']}%"
        values.extend([needle, needle, needle])
    where = f"where {' and '.join(clauses)}" if clauses else ""
    with connect() as connection:
        rows = connection.execute(
            f"select * from link_deals {where} order by updated_at desc, id desc",
            values,
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_deal(deal_id: int) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        row = connection.execute("select * from link_deals where id = ?", (deal_id,)).fetchone()
    return row_to_dict(row)


def update_deal(deal_id: int, fields: dict[str, str]) -> None:
    init_db()
    allowed = {
        "deal_status",
        "placement_type",
        "price_amount",
        "price_currency",
        "price_notes",
        "domain_trust",
        "domain_trust_source",
        "target_url",
        "publisher_url",
        "writing_requirements",
        "link_requirements",
        "turnaround_time",
        "additional_notes",
    }
    updates: dict[str, Any] = {key: fields.get(key, "") for key in allowed if key in fields}
    if "deal_status" in updates and updates["deal_status"] not in DEAL_STATUSES:
        updates["deal_status"] = "needs_review"
    if "placement_type" in updates and updates["placement_type"] not in PLACEMENT_TYPES:
        updates["placement_type"] = "other"
    if "price_amount" in updates:
        raw = str(updates["price_amount"]).strip()
        updates["price_amount"] = float(raw) if raw else None
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connect() as connection:
        connection.execute(f"update link_deals set {assignments} where id=?", [*updates.values(), deal_id])
        connection.commit()


def recent_reply_sync(limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = connection.execute(
            "select * from instantly_reply_sync order by received_at desc, id desc limit ?",
            (limit,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def html_escape(value: Any) -> str:
    return escape("" if value is None else str(value))
