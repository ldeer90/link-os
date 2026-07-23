from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

from .db import connect, init_db
from .utils import now_iso


DOMAIN_COLUMNS = (
    "root_domain",
    "domain",
    "website",
    "site",
    "url",
    "source_url",
    "referring_domain",
    "source_domain",
    "publisher_url",
)
EMAIL_COLUMNS = ("contact_email", "email", "best_contact_email", "publisher_email")
SITE_NAME_COLUMNS = ("site_name", "company_name", "publisher_name", "name")
NICHE_COLUMNS = ("niche", "industry", "category", "topic")
OPPORTUNITY_COLUMNS = ("opportunity_type", "placement_type", "prospect_type")


@dataclass(frozen=True)
class IntakeRow:
    input_value: str
    root_domain: str
    source_url: str = ""
    contact_email: str = ""
    site_name: str = ""
    niche: str = ""
    opportunity_type: str = ""
    notes: str = ""


def normalize_domain(value: str) -> str:
    item = (value or "").strip().strip("\"'<>[]()")
    if not item:
        return ""
    if "@" in item and not item.startswith(("http://", "https://")):
        item = item.rsplit("@", 1)[-1]
    parsed = urlparse(item if "://" in item else f"https://{item}")
    host = parsed.hostname or ""
    return host.lower().removeprefix("www.").strip(".")


def valid_domain(domain: str) -> bool:
    if not domain or len(domain) > 253 or "." not in domain:
        return False
    labels = domain.split(".")
    if len(labels[-1]) < 2:
        return False
    return all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[a-z0-9-]+", label) is not None
        for label in labels
    )


def _first(row: dict[str, Any], columns: Iterable[str]) -> str:
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for column in columns:
        value = str(normalized.get(column) or "").strip()
        if value:
            return value
    return ""


def rows_from_paste(text: str) -> list[IntakeRow]:
    values = re.split(r"[\s,;]+", text or "")
    rows: list[IntakeRow] = []
    for value in values:
        cleaned = value.strip().strip("\"'<>[]()")
        if not cleaned:
            continue
        domain = normalize_domain(cleaned)
        source_url = cleaned if cleaned.startswith(("http://", "https://")) else ""
        contact_email = cleaned.lower() if "@" in cleaned and not source_url else ""
        rows.append(IntakeRow(cleaned, domain, source_url=source_url, contact_email=contact_email))
    return rows


def rows_from_csv(content: bytes) -> list[IntakeRow]:
    if not content:
        return []
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return rows_from_paste(text)
    rows: list[IntakeRow] = []
    for raw in reader:
        input_value = _first(raw, DOMAIN_COLUMNS)
        if not input_value:
            continue
        rows.append(
            IntakeRow(
                input_value=input_value,
                root_domain=normalize_domain(input_value),
                source_url=input_value if input_value.startswith(("http://", "https://")) else _first(raw, ("source_url", "publisher_url", "url")),
                contact_email=_first(raw, EMAIL_COLUMNS).lower(),
                site_name=_first(raw, SITE_NAME_COLUMNS),
                niche=_first(raw, NICHE_COLUMNS),
                opportunity_type=_first(raw, OPPORTUNITY_COLUMNS),
                notes=_first(raw, ("notes", "why_relevant", "evidence", "matched_phrase")),
            )
        )
    return rows


def create_import(
    rows: list[IntakeRow],
    *,
    source_type: str,
    source_name: str,
    queue: bool = False,
) -> dict[str, Any]:
    init_db()
    timestamp = now_iso()
    seen: set[str] = set()
    accepted_domains: list[str] = []
    counts = {"total_rows": len(rows), "accepted_rows": 0, "duplicate_rows": 0, "rejected_rows": 0, "queued_rows": 0}
    row_results: list[dict[str, Any]] = []

    with connect() as connection:
        cursor = connection.execute(
            """
            insert into prospect_imports (source_type, source_name, status, total_rows, created_at)
            values (?, ?, 'processing', ?, ?)
            """,
            (source_type, source_name, len(rows), timestamp),
        )
        import_id = int(cursor.lastrowid)
        for row in rows:
            status = "accepted"
            reason = ""
            if not valid_domain(row.root_domain):
                status, reason = "rejected", "Invalid or missing domain"
                counts["rejected_rows"] += 1
            elif row.root_domain in seen:
                status, reason = "duplicate", "Repeated in this import"
                counts["duplicate_rows"] += 1
            else:
                seen.add(row.root_domain)
                existing = connection.execute("select id from prospect_records where root_domain=?", (row.root_domain,)).fetchone()
                if existing:
                    status, reason = "duplicate", "Already in Link OS"
                    counts["duplicate_rows"] += 1
                    connection.execute(
                        """
                        update prospect_records set
                            site_name=case when site_name='' then ? else site_name end,
                            source_url=case when source_url='' then ? else source_url end,
                            contact_email=case when contact_email='' then ? else contact_email end,
                            niche=case when niche='' then ? else niche end,
                            opportunity_type=case when opportunity_type='' then ? else opportunity_type end,
                            last_seen_at=?
                        where root_domain=?
                        """,
                        (row.site_name, row.source_url, row.contact_email, row.niche, row.opportunity_type, timestamp, row.root_domain),
                    )
                else:
                    connection.execute(
                        """
                        insert into prospect_records (
                            root_domain, site_name, source_url, contact_email, niche, opportunity_type,
                            lifecycle_stage, source_name, notes, first_seen_at, last_seen_at
                        ) values (?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?, ?)
                        """,
                        (
                            row.root_domain,
                            row.site_name,
                            row.source_url,
                            row.contact_email,
                            row.niche,
                            row.opportunity_type,
                            source_name,
                            row.notes,
                            timestamp,
                            timestamp,
                        ),
                    )
                    accepted_domains.append(row.root_domain)
                    counts["accepted_rows"] += 1
            connection.execute(
                """
                insert into prospect_import_rows (
                    import_id, input_value, root_domain, source_url, contact_email,
                    row_status, reason, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (import_id, row.input_value, row.root_domain, row.source_url, row.contact_email, status, reason, timestamp),
            )
            row_results.append({"input_value": row.input_value, "root_domain": row.root_domain, "status": status, "reason": reason})
        connection.commit()

    queue_result: dict[str, Any] = {}
    if queue and accepted_domains:
        from .prospecting_bridge import queue_domains

        queue_result = queue_domains(accepted_domains, source_label=f"link_os_import_{import_id}")
        counts["queued_rows"] = len(accepted_domains)
        with connect() as connection:
            placeholders = ",".join("?" for _ in accepted_domains)
            connection.execute(
                f"update prospect_records set lifecycle_stage='queued', last_seen_at=? where root_domain in ({placeholders})",
                [now_iso(), *accepted_domains],
            )
            connection.commit()

    summary = {**counts, "import_id": import_id, "accepted_domains": accepted_domains, "queue_result": queue_result}
    with connect() as connection:
        connection.execute(
            """
            update prospect_imports set status=?, accepted_rows=?, duplicate_rows=?, rejected_rows=?,
                queued_rows=?, summary_json=? where id=?
            """,
            (
                "queued" if queue and accepted_domains else "imported",
                counts["accepted_rows"],
                counts["duplicate_rows"],
                counts["rejected_rows"],
                counts["queued_rows"],
                json.dumps(summary, sort_keys=True),
                import_id,
            ),
        )
        connection.commit()
    return {**summary, "rows": row_results}


def intake_dashboard(limit: int = 60) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        totals = {
            "prospects": connection.execute("select count(*) from prospect_records").fetchone()[0],
            "queued": connection.execute("select count(*) from prospect_records where lifecycle_stage='queued'").fetchone()[0],
            "with_email": connection.execute("select count(*) from prospect_records where contact_email!=''").fetchone()[0],
            "imports": connection.execute("select count(*) from prospect_imports").fetchone()[0],
        }
        imports = [dict(row) for row in connection.execute("select * from prospect_imports order by id desc limit 12").fetchall()]
        prospects = [dict(row) for row in connection.execute("select * from prospect_records order by last_seen_at desc limit ?", (limit,)).fetchall()]
    return {"totals": totals, "imports": imports, "prospects": prospects}


def import_detail(import_id: int) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        batch = connection.execute("select * from prospect_imports where id=?", (import_id,)).fetchone()
        rows = connection.execute("select * from prospect_import_rows where import_id=? order by id", (import_id,)).fetchall()
    return {"import": dict(batch) if batch else {}, "rows": [dict(row) for row in rows]}
