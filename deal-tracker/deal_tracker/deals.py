from __future__ import annotations

import json
import re
from html import escape
from typing import Any

from .classifier import DEAL_STATUSES, PLACEMENT_TYPES, PriceOption, classify_reply
from .db import assign_missing_publisher_entities, connect, init_db
from .fx import convert_to_aud
from .instantly import DEFAULT_CAMPAIGN_IDS, list_campaign_emails, list_campaign_leads
from .utils import clean_domain, now_iso


INBOUND_UE_TYPE = 2


def tld_for_domain(root_domain: str) -> str:
    parts = (root_domain or "").split(".")
    if len(parts) >= 3 and parts[-1] == "au":
        return ".".join(parts[-2:])
    return parts[-1] if len(parts) > 1 else ""


def price_band_for(amount: float | None) -> str:
    if amount is None:
        return "Ask"
    if amount < 150:
        return "$"
    if amount < 500:
        return "$$"
    return "$$$"


def email_body_text(email: dict[str, Any]) -> str:
    body = email.get("body")
    if isinstance(body, dict):
        if body.get("text"):
            return str(body["text"]).strip()
        html = str(body.get("html") or "")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return str(body or "").strip()


def inbound_replies(campaign_id: str) -> list[dict[str, Any]]:
    return [email for email in list_campaign_emails(campaign_id) if email.get("ue_type") == INBOUND_UE_TYPE]


def lead_context(lead: dict[str, Any] | None, email: dict[str, Any]) -> dict[str, str]:
    lead = lead or {}
    payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
    lead_email = str(email.get("lead") or lead.get("email") or "").lower()
    website = str(lead.get("website") or payload.get("website") or "")
    root_domain = str(payload.get("root_domain") or clean_domain(website) or clean_domain(lead_email))
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
                str(email.get("subject") or ""),
                str(email.get("from_address_email") or ""),
                str(email.get("to_address_email_list") or ""),
                body_text,
                str(email.get("timestamp_email") or email.get("timestamp_created") or ""),
                extraction.classification,
                extraction.to_json(),
                processed_at,
            ),
        )
        created_deal = False
        if extraction.create_deal:
            price_options: list[PriceOption | None] = extraction.price_options or [None]
            for price_option in price_options:
                root_domain = price_option.root_domain if price_option else context["root_domain"]
                site_name = price_option.site_name if price_option else context["site_name"]
                price_amount = price_option.price_amount if price_option else extraction.price_amount
                price_currency = price_option.price_currency if price_option else extraction.price_currency
                price_notes = price_option.price_notes if price_option else extraction.price_notes
                publisher_url = f"https://{root_domain}" if price_option and root_domain else extraction.publisher_url or context["publisher_url"]
                existing = connection.execute(
                    "select id from link_deals where instantly_thread_id = ? and contact_email = ? and root_domain = ?",
                    (thread_id, context["contact_email"], root_domain),
                ).fetchone()
                values = (
                    root_domain,
                    site_name,
                    campaign_id,
                    context["instantly_lead_id"],
                    extraction.deal_status,
                    extraction.placement_type,
                    price_amount,
                    price_currency,
                    price_notes,
                    publisher_url,
                    extraction.writing_requirements,
                    extraction.link_requirements,
                    extraction.turnaround_time,
                    extraction.price_notes,
                    extraction.reply_summary,
                    body_text,
                    processed_at,
                    tld_for_domain(root_domain),
                )
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
                            additional_notes=coalesce(nullif(?, ''), additional_notes),
                            reply_summary=?, reply_evidence_text=?, updated_at=?,
                            tld=case when tld='' then ? else tld end,
                            publisher_cost_amount=coalesce(publisher_cost_amount, ?),
                            publisher_cost_currency=coalesce(nullif(publisher_cost_currency, ''), ?),
                            link_insertion_cost_amount=coalesce(link_insertion_cost_amount, ?),
                            link_insertion_cost_currency=coalesce(nullif(link_insertion_cost_currency, ''), ?),
                            link_insertion_notes=coalesce(nullif(?, ''), link_insertion_notes),
                            last_seen_from_instantly=?
                        where id=?
                        """,
                        (*values, price_amount, price_currency, extraction.link_insertion_cost_amount, extraction.link_insertion_cost_currency, extraction.link_insertion_notes, processed_at, existing["id"]),
                    )
                else:
                    connection.execute(
                        """
                        insert into link_deals (
                            root_domain, site_name, contact_email, source_campaign_id, instantly_lead_id,
                            instantly_thread_id, deal_status, placement_type, price_amount, price_currency,
                            price_notes, domain_trust_source, publisher_url, writing_requirements,
                            link_requirements, turnaround_time, additional_notes, reply_summary,
                            reply_evidence_text, created_at, updated_at, industry, tld,
                            publisher_cost_amount, publisher_cost_currency,
                            link_insertion_cost_amount, link_insertion_cost_currency,
                            link_insertion_notes, link_insertion_reseller_price_currency,
                            reseller_price_currency, price_band,
                            visibility_min_tier, is_listed, admin_review_status,
                            last_seen_from_instantly
                        )
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            root_domain,
                            site_name,
                            context["contact_email"],
                            campaign_id,
                            context["instantly_lead_id"],
                            thread_id,
                            extraction.deal_status,
                            extraction.placement_type,
                            price_amount,
                            price_currency,
                            price_notes,
                            "manual_seranking_later",
                            publisher_url,
                            extraction.writing_requirements,
                            extraction.link_requirements,
                            extraction.turnaround_time,
                            extraction.price_notes,
                            extraction.reply_summary,
                            body_text,
                            processed_at,
                            processed_at,
                            "",
                            tld_for_domain(root_domain),
                            price_amount,
                            price_currency,
                            extraction.link_insertion_cost_amount,
                            extraction.link_insertion_cost_currency,
                            extraction.link_insertion_notes,
                            "AUD",
                            "AUD",
                            "Ask",
                            "basic",
                            0,
                            "needs_review",
                            processed_at,
                        ),
                    )
                    created_deal = True
            assign_missing_publisher_entities(connection)
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
    if filters.get("admin_review_status"):
        clauses.append("admin_review_status = ?")
        values.append(filters["admin_review_status"])
    if filters.get("listed") == "yes":
        clauses.append("is_listed = 1")
    if filters.get("listed") == "no":
        clauses.append("is_listed = 0")
    if filters.get("needs_reseller_price") == "yes":
        clauses.append("reseller_price_amount is null")
    if filters.get("needs_domain_trust") == "yes":
        clauses.append("(domain_trust = '' or domain_trust is null)")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    with connect() as connection:
        rows = connection.execute(f"select * from link_deals {where} order by updated_at desc, id desc", values).fetchall()
    return [dict(row) for row in rows]


def get_deal(deal_id: int) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        row = connection.execute("select * from link_deals where id = ?", (deal_id,)).fetchone()
    return dict(row) if row else {}


def update_deal(deal_id: int, fields: dict[str, str]) -> None:
    init_db()
    allowed = {
        "deal_status",
        "publisher_entity_id",
        "placement_type",
        "price_amount",
        "price_currency",
        "price_notes",
        "domain_trust",
        "domain_trust_source",
        "target_url",
        "publisher_url",
        "industry",
        "tld",
        "quality_notes",
        "publisher_cost_amount",
        "publisher_cost_currency",
        "reseller_price_amount",
        "reseller_price_currency",
        "link_insertion_cost_amount",
        "link_insertion_cost_currency",
        "link_insertion_reseller_price_amount",
        "link_insertion_reseller_price_currency",
        "link_insertion_notes",
        "price_band",
        "visibility_min_tier",
        "is_listed",
        "admin_review_status",
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
    if "publisher_entity_id" in updates:
        raw = str(updates["publisher_entity_id"]).strip()
        updates["publisher_entity_id"] = int(raw) if raw.isdigit() else None
    if "price_amount" in updates:
        raw = str(updates["price_amount"]).strip()
        updates["price_amount"] = float(raw) if raw else None
    if "publisher_cost_amount" in updates:
        raw = str(updates["publisher_cost_amount"]).strip()
        updates["publisher_cost_amount"] = float(raw) if raw else None
    if "reseller_price_amount" in updates:
        raw = str(updates["reseller_price_amount"]).strip()
        updates["reseller_price_amount"] = float(raw) if raw else None
        updates["price_band"] = price_band_for(updates["reseller_price_amount"])
    if "link_insertion_cost_amount" in updates:
        raw = str(updates["link_insertion_cost_amount"]).strip()
        updates["link_insertion_cost_amount"] = float(raw) if raw else None
    if "link_insertion_reseller_price_amount" in updates:
        raw = str(updates["link_insertion_reseller_price_amount"]).strip()
        updates["link_insertion_reseller_price_amount"] = float(raw) if raw else None
    if "is_listed" in updates:
        updates["is_listed"] = 1 if str(updates["is_listed"]) in {"1", "true", "on", "yes"} else 0
    if "visibility_min_tier" in updates and updates["visibility_min_tier"] not in {"basic", "trusted", "full"}:
        updates["visibility_min_tier"] = "basic"
    if "admin_review_status" in updates and updates["admin_review_status"] not in {"needs_review", "approved", "rejected", "archived"}:
        updates["admin_review_status"] = "needs_review"
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connect() as connection:
        connection.execute(f"update link_deals set {assignments} where id=?", [*updates.values(), deal_id])
        connection.commit()


def _aud_amount(amount: Any, currency: Any) -> float | None:
    if amount is None or amount == "":
        return None
    try:
        if str(currency or "AUD").upper() == "AUD":
            return float(amount)
        return convert_to_aud(amount, currency)
    except Exception:
        return None


def _average(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _money_label(value: float | None) -> str:
    return "" if value is None else f"AUD {value:,.2f}"


def _domain_trust_bucket(value: Any) -> str:
    try:
        score = float(str(value or "").strip())
    except ValueError:
        return "Missing"
    if score < 20:
        return "0-19"
    if score < 40:
        return "20-39"
    if score < 60:
        return "40-59"
    return "60+"


def _restrictions_marker(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("writing_requirements", "link_requirements", "link_insertion_notes", "additional_notes", "quality_notes")
    ).lower()
    flags = []
    for term in ("casino", "gambling", "cbd", "crypto", "adult", "sponsored", "paypal", "invoice"):
        if term in text:
            flags.append(term)
    return ", ".join(dict.fromkeys(flags))


def _summarize_deals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    publisher_costs = [value for row in rows if (value := _aud_amount(row.get("publisher_cost_amount"), row.get("publisher_cost_currency"))) is not None]
    reseller_prices = [value for row in rows if (value := _aud_amount(row.get("reseller_price_amount"), row.get("reseller_price_currency"))) is not None]
    link_costs = [value for row in rows if (value := _aud_amount(row.get("link_insertion_cost_amount"), row.get("link_insertion_cost_currency"))) is not None]
    link_resellers = [
        value
        for row in rows
        if (value := _aud_amount(row.get("link_insertion_reseller_price_amount"), row.get("link_insertion_reseller_price_currency"))) is not None
    ]
    margins = []
    for row in rows:
        cost = _aud_amount(row.get("publisher_cost_amount"), row.get("publisher_cost_currency"))
        price = _aud_amount(row.get("reseller_price_amount"), row.get("reseller_price_currency"))
        if cost is not None and price is not None:
            margins.append(price - cost)
    total_reseller = sum(reseller_prices)
    total_margin = sum(margins)
    return {
        "domain_count": len({row.get("root_domain") for row in rows if row.get("root_domain")}),
        "priced_count": len(publisher_costs),
        "avg_publisher_cost_aud": _average(publisher_costs),
        "avg_reseller_price_aud": _average(reseller_prices),
        "avg_margin_aud": _average(margins),
        "margin_pct": (total_margin / total_reseller * 100) if total_reseller else None,
        "avg_link_insertion_cost_aud": _average(link_costs),
        "avg_link_insertion_reseller_aud": _average(link_resellers),
    }


def _count_by(rows: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        key = key_fn(row) or "Unset"
        counts[str(key)] = counts.get(str(key), 0) + 1
    return [{"label": key, "count": counts[key]} for key in sorted(counts, key=lambda item: (-counts[item], item))]


def list_publisher_entities() -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        assign_missing_publisher_entities(connection)
        rows = connection.execute(
            """
            select publisher_entities.*,
                   count(link_deals.id) as domain_count,
                   sum(case when link_deals.is_listed=1 and link_deals.admin_review_status='approved' then 1 else 0 end) as listed_count
            from publisher_entities
            left join link_deals on link_deals.publisher_entity_id=publisher_entities.id
            group by publisher_entities.id
            order by listed_count desc, domain_count desc, name
            """
        ).fetchall()
        connection.commit()
    return [dict(row) for row in rows]


def get_publisher_entity(entity_id: int) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        row = connection.execute("select * from publisher_entities where id=?", (entity_id,)).fetchone()
    return dict(row) if row else {}


def update_publisher_entity(entity_id: int, fields: dict[str, str]) -> None:
    init_db()
    allowed = {"name", "primary_email", "primary_domain", "notes"}
    updates = {key: fields.get(key, "").strip() for key in allowed if key in fields}
    if not updates:
        return
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connect() as connection:
        connection.execute(f"update publisher_entities set {assignments} where id=?", [*updates.values(), entity_id])
        connection.commit()


def merge_publisher_entities(source_entity_id: int, target_entity_id: int) -> None:
    if source_entity_id == target_entity_id:
        return
    init_db()
    with connect() as connection:
        target = connection.execute("select id from publisher_entities where id=?", (target_entity_id,)).fetchone()
        source = connection.execute("select id from publisher_entities where id=?", (source_entity_id,)).fetchone()
        if not target or not source:
            return
        connection.execute("update link_deals set publisher_entity_id=?, updated_at=? where publisher_entity_id=?", (target_entity_id, now_iso(), source_entity_id))
        connection.execute("delete from publisher_entities where id=?", (source_entity_id,))
        connection.commit()


def scorecard_data() -> dict[str, Any]:
    init_db()
    with connect() as connection:
        assign_missing_publisher_entities(connection)
        rows = [dict(row) for row in connection.execute("select * from link_deals").fetchall()]
        entities = [dict(row) for row in connection.execute("select * from publisher_entities").fetchall()]
        connection.commit()

    listed_rows = [row for row in rows if row.get("is_listed") and row.get("admin_review_status") == "approved"]
    priced_rows = [row for row in listed_rows if row.get("publisher_cost_amount") is not None]
    totals = _summarize_deals(listed_rows)
    totals.update(
        {
            "total_domains": len({row.get("root_domain") for row in rows if row.get("root_domain")}),
            "listed_domains": len({row.get("root_domain") for row in listed_rows if row.get("root_domain")}),
            "managing_entities": len(entities),
            "priced_domains": len({row.get("root_domain") for row in priced_rows if row.get("root_domain")}),
            "missing_reseller_price": sum(1 for row in listed_rows if row.get("reseller_price_amount") is None),
            "missing_domain_trust": sum(1 for row in listed_rows if not str(row.get("domain_trust") or "").strip()),
            "needs_review": sum(1 for row in rows if row.get("admin_review_status") == "needs_review"),
        }
    )

    entity_by_id = {entity["id"]: entity for entity in entities}
    entity_rows: list[dict[str, Any]] = []
    for entity_id in sorted(entity_by_id):
        scoped = [row for row in listed_rows if row.get("publisher_entity_id") == entity_id]
        if not scoped:
            continue
        summary = _summarize_deals(scoped)
        prices = [(row, _aud_amount(row.get("reseller_price_amount"), row.get("reseller_price_currency"))) for row in scoped]
        priced_pairs = [(row, value) for row, value in prices if value is not None]
        lowest = min(priced_pairs, key=lambda item: item[1])[0] if priced_pairs else {}
        highest = max(priced_pairs, key=lambda item: item[1])[0] if priced_pairs else {}
        entity = entity_by_id[entity_id]
        entity_rows.append(
            {
                **summary,
                "id": entity_id,
                "name": entity.get("name") or "Publisher",
                "primary_email": entity.get("primary_email") or "",
                "primary_domain": entity.get("primary_domain") or "",
                "missing_domain_trust": sum(1 for row in scoped if not str(row.get("domain_trust") or "").strip()),
                "lowest_domain": lowest.get("root_domain", ""),
                "highest_domain": highest.get("root_domain", ""),
                "restrictions": ", ".join(filter(None, dict.fromkeys(_restrictions_marker(row) for row in scoped))),
            }
        )
    entity_rows.sort(key=lambda row: (-int(row.get("domain_count") or 0), str(row.get("name") or "")))

    low_margin = []
    highest_margin = []
    for row in listed_rows:
        cost = _aud_amount(row.get("publisher_cost_amount"), row.get("publisher_cost_currency"))
        price = _aud_amount(row.get("reseller_price_amount"), row.get("reseller_price_currency"))
        if cost is None or price is None:
            continue
        margin = price - cost
        item = {**row, "publisher_cost_aud": cost, "reseller_price_aud": price, "margin_aud": margin}
        if margin <= 25:
            low_margin.append(item)
        highest_margin.append(item)
    low_margin.sort(key=lambda row: (row["margin_aud"], row.get("root_domain") or ""))
    highest_margin.sort(key=lambda row: (-row["margin_aud"], row.get("root_domain") or ""))

    cheapest = [
        {**row, "publisher_cost_aud": value}
        for row in listed_rows
        if (value := _aud_amount(row.get("publisher_cost_amount"), row.get("publisher_cost_currency"))) is not None
    ]
    cheapest.sort(key=lambda row: (row["publisher_cost_aud"], row.get("root_domain") or ""))

    return {
        "totals": totals,
        "entities": entity_rows,
        "breakdowns": {
            "tld": _count_by(listed_rows, lambda row: row.get("tld")),
            "industry": _count_by(listed_rows, lambda row: row.get("industry")),
            "placement_type": _count_by(listed_rows, lambda row: row.get("placement_type")),
            "price_band": _count_by(listed_rows, lambda row: row.get("price_band")),
            "domain_trust": _count_by(listed_rows, lambda row: _domain_trust_bucket(row.get("domain_trust"))),
            "listing": _count_by(rows, lambda row: "Listed" if row.get("is_listed") else "Unlisted"),
        },
        "flags": {
            "missing_reseller_price": [row for row in listed_rows if row.get("reseller_price_amount") is None],
            "missing_domain_trust": [row for row in listed_rows if not str(row.get("domain_trust") or "").strip()],
            "low_margin": low_margin[:20],
            "highest_margin": highest_margin[:20],
            "cheapest": cheapest[:20],
            "link_insertions": [row for row in listed_rows if row.get("link_insertion_cost_amount") is not None][:30],
            "high_concentration": [row for row in entity_rows if int(row.get("domain_count") or 0) >= 10],
            "recent_needs_review": sorted([row for row in rows if row.get("admin_review_status") == "needs_review"], key=lambda row: row.get("updated_at") or "", reverse=True)[:20],
        },
    }


def publisher_entity_detail(entity_id: int) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        entity = connection.execute("select * from publisher_entities where id=?", (entity_id,)).fetchone()
        if not entity:
            return {}
        rows = [dict(row) for row in connection.execute("select * from link_deals where publisher_entity_id=? order by is_listed desc, root_domain", (entity_id,)).fetchall()]
    listed_rows = [row for row in rows if row.get("is_listed") and row.get("admin_review_status") == "approved"]
    return {"entity": dict(entity), "deals": rows, "summary": _summarize_deals(listed_rows)}


def recent_reply_sync(limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = connection.execute("select * from instantly_reply_sync order by received_at desc, id desc limit ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def dashboard_stats() -> dict[str, Any]:
    init_db()
    with connect() as connection:
        return {
            "total_deals": connection.execute("select count(*) from link_deals").fetchone()[0],
            "needs_review": connection.execute("select count(*) from link_deals where admin_review_status='needs_review'").fetchone()[0],
            "listed": connection.execute("select count(*) from link_deals where is_listed=1").fetchone()[0],
            "missing_reseller_price": connection.execute("select count(*) from link_deals where reseller_price_amount is null").fetchone()[0],
            "missing_domain_trust": connection.execute("select count(*) from link_deals where domain_trust='' or domain_trust is null").fetchone()[0],
            "recent_enquiries": connection.execute("select count(*) from agency_enquiries where status='new'").fetchone()[0],
        }


def list_sync_runs(limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = connection.execute("select * from sync_runs order by started_at desc, id desc limit ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def list_catalogue_deals(user: dict[str, Any], filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    init_db()
    filters = filters or {}
    tier_rank = {"basic": 1, "trusted": 2, "full": 3}.get(str(user.get("visibility_tier") or "basic"), 1)
    clauses = ["is_listed=1", "admin_review_status='approved'", "case visibility_min_tier when 'full' then 3 when 'trusted' then 2 else 1 end <= ?"]
    values: list[Any] = [tier_rank]
    if filters.get("q"):
        clauses.append("(root_domain like ? or site_name like ? or industry like ? or tld like ?)")
        needle = f"%{filters['q']}%"
        values.extend([needle, needle, needle, needle])
    if filters.get("industry"):
        clauses.append("industry like ?")
        values.append(f"%{filters['industry']}%")
    if filters.get("tld"):
        clauses.append("tld = ?")
        values.append(filters["tld"])
    where = "where " + " and ".join(clauses)
    with connect() as connection:
        rows = connection.execute(f"select * from link_deals {where} order by reseller_price_amount is null, reseller_price_amount, root_domain", values).fetchall()
    return [dict(row) for row in rows]


def get_catalogue_deal(deal_id: int, user: dict[str, Any]) -> dict[str, Any]:
    deal = get_deal(deal_id)
    if not deal:
        return {}
    if not deal.get("is_listed") or deal.get("admin_review_status") != "approved":
        return {}
    tier_rank = {"basic": 1, "trusted": 2, "full": 3}.get(str(user.get("visibility_tier") or "basic"), 1)
    required_rank = {"basic": 1, "trusted": 2, "full": 3}.get(str(deal.get("visibility_min_tier") or "basic"), 1)
    return deal if tier_rank >= required_rank else {}


def create_agency_enquiry(user_id: int, deal_id: int, message: str) -> int:
    init_db()
    with connect() as connection:
        cursor = connection.execute(
            "insert into agency_enquiries (user_id, deal_id, message, created_at, status) values (?, ?, ?, ?, 'new')",
            (user_id, deal_id, message.strip(), now_iso()),
        )
        connection.commit()
        return int(cursor.lastrowid)


def list_agency_enquiries(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = connection.execute(
            """
            select agency_enquiries.*, users.email as agency_email, link_deals.root_domain, link_deals.site_name
            from agency_enquiries
            join users on users.id=agency_enquiries.user_id
            join link_deals on link_deals.id=agency_enquiries.deal_id
            order by agency_enquiries.created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def html_escape(value: Any) -> str:
    return escape("" if value is None else str(value))
