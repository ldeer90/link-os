from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import DB_PATH, ensure_dirs


SCHEMA = """
create table if not exists publisher_entities (
    id integer primary key autoincrement,
    name text not null default '',
    primary_email text not null default '',
    primary_domain text not null default '',
    group_key text not null unique default '',
    notes text not null default '',
    created_at text not null,
    updated_at text not null
);

create index if not exists idx_publisher_entities_domain on publisher_entities(primary_domain);

create table if not exists link_deals (
    id integer primary key autoincrement,
    publisher_entity_id integer,
    root_domain text not null default '',
    site_name text not null default '',
    contact_email text not null default '',
    source_campaign_id text not null default '',
    instantly_lead_id text not null default '',
    instantly_thread_id text not null default '',
    deal_status text not null default 'needs_review',
    placement_type text not null default 'other',
    price_amount real,
    price_currency text not null default 'AUD',
    price_notes text not null default '',
    domain_trust text not null default '',
    domain_trust_source text not null default 'manual_seranking_later',
    target_url text not null default '',
    publisher_url text not null default '',
    writing_requirements text not null default '',
    link_requirements text not null default '',
    turnaround_time text not null default '',
    additional_notes text not null default '',
    reply_summary text not null default '',
    reply_evidence_text text not null default '',
    industry text not null default '',
    tld text not null default '',
    quality_notes text not null default '',
    publisher_cost_amount real,
    publisher_cost_currency text not null default 'AUD',
    reseller_price_amount real,
    reseller_price_currency text not null default 'AUD',
    link_insertion_cost_amount real,
    link_insertion_cost_currency text not null default 'AUD',
    link_insertion_reseller_price_amount real,
    link_insertion_reseller_price_currency text not null default 'AUD',
    link_insertion_notes text not null default '',
    price_band text not null default 'Ask',
    visibility_min_tier text not null default 'basic',
    is_listed integer not null default 0,
    admin_review_status text not null default 'needs_review',
    last_seen_from_instantly text not null default '',
    created_at text not null,
    updated_at text not null,
    foreign key(publisher_entity_id) references publisher_entities(id)
);

create index if not exists idx_link_deals_status on link_deals(deal_status);
create index if not exists idx_link_deals_domain on link_deals(root_domain);
drop index if exists idx_link_deals_thread_contact;
create unique index if not exists idx_link_deals_thread_contact_domain on link_deals(instantly_thread_id, contact_email, root_domain);

create table if not exists instantly_reply_sync (
    id integer primary key autoincrement,
    campaign_id text not null,
    email_id text not null,
    lead_email text not null default '',
    thread_id text not null default '',
    subject text not null default '',
    from_email text not null default '',
    to_email text not null default '',
    body_text text not null default '',
    received_at text not null default '',
    classification text not null default '',
    extracted_json text not null default '{}',
    raw_json text not null default '{}',
    processed_at text not null,
    unique(email_id)
);

create index if not exists idx_reply_sync_campaign on instantly_reply_sync(campaign_id);
create index if not exists idx_reply_sync_lead on instantly_reply_sync(lead_email);
create index if not exists idx_reply_sync_classification on instantly_reply_sync(classification);

create table if not exists users (
    id integer primary key autoincrement,
    email text not null unique,
    password_hash text not null,
    role text not null default 'agency',
    visibility_tier text not null default 'basic',
    is_active integer not null default 1,
    created_at text not null,
    updated_at text not null,
    last_login_at text not null default ''
);

create index if not exists idx_users_role on users(role);
create index if not exists idx_users_active on users(is_active);

create table if not exists user_sessions (
    id integer primary key autoincrement,
    user_id integer not null,
    token_hash text not null unique,
    created_at text not null,
    expires_at text not null,
    foreign key(user_id) references users(id)
);

create index if not exists idx_user_sessions_user on user_sessions(user_id);
create index if not exists idx_user_sessions_expires on user_sessions(expires_at);

create table if not exists agency_enquiries (
    id integer primary key autoincrement,
    user_id integer not null,
    deal_id integer not null,
    message text not null default '',
    created_at text not null,
    status text not null default 'new',
    foreign key(user_id) references users(id),
    foreign key(deal_id) references link_deals(id)
);

create index if not exists idx_agency_enquiries_user on agency_enquiries(user_id);
create index if not exists idx_agency_enquiries_deal on agency_enquiries(deal_id);

create table if not exists prospect_imports (
    id integer primary key autoincrement,
    source_type text not null default 'paste',
    source_name text not null default '',
    status text not null default 'imported',
    total_rows integer not null default 0,
    accepted_rows integer not null default 0,
    duplicate_rows integer not null default 0,
    rejected_rows integer not null default 0,
    queued_rows integer not null default 0,
    summary_json text not null default '{}',
    created_at text not null
);

create index if not exists idx_prospect_imports_created on prospect_imports(created_at);

create table if not exists prospect_records (
    id integer primary key autoincrement,
    root_domain text not null unique,
    site_name text not null default '',
    source_url text not null default '',
    contact_email text not null default '',
    niche text not null default '',
    opportunity_type text not null default '',
    lifecycle_stage text not null default 'imported',
    source_name text not null default '',
    notes text not null default '',
    first_seen_at text not null,
    last_seen_at text not null
);

create index if not exists idx_prospect_records_stage on prospect_records(lifecycle_stage);
create index if not exists idx_prospect_records_domain on prospect_records(root_domain);

create table if not exists prospect_import_rows (
    id integer primary key autoincrement,
    import_id integer not null,
    input_value text not null default '',
    root_domain text not null default '',
    source_url text not null default '',
    contact_email text not null default '',
    row_status text not null default 'accepted',
    reason text not null default '',
    created_at text not null,
    foreign key(import_id) references prospect_imports(id)
);

create index if not exists idx_prospect_import_rows_import on prospect_import_rows(import_id);
create index if not exists idx_prospect_import_rows_domain on prospect_import_rows(root_domain);

create table if not exists sync_runs (
    id integer primary key autoincrement,
    started_at text not null,
    finished_at text not null default '',
    status text not null default 'running',
    trigger_type text not null default 'manual',
    inbound_reply_count integer not null default 0,
    new_deal_count integer not null default 0,
    error_message text not null default '',
    summary_json text not null default '{}'
);

create index if not exists idx_sync_runs_started on sync_runs(started_at);
create index if not exists idx_sync_runs_status on sync_runs(status);

create table if not exists sync_events (
    id integer primary key autoincrement,
    sync_run_id integer not null,
    event_type text not null,
    message text not null default '',
    created_at text not null,
    foreign key(sync_run_id) references sync_runs(id)
);

create index if not exists idx_sync_events_run on sync_events(sync_run_id);

create table if not exists app_settings (
    key text primary key,
    value text not null default '',
    updated_at text not null
);
"""


LINK_DEAL_EXTRA_COLUMNS = {
    "publisher_entity_id": "integer",
    "industry": "text not null default ''",
    "tld": "text not null default ''",
    "quality_notes": "text not null default ''",
    "publisher_cost_amount": "real",
    "publisher_cost_currency": "text not null default 'AUD'",
    "reseller_price_amount": "real",
    "reseller_price_currency": "text not null default 'AUD'",
    "link_insertion_cost_amount": "real",
    "link_insertion_cost_currency": "text not null default 'AUD'",
    "link_insertion_reseller_price_amount": "real",
    "link_insertion_reseller_price_currency": "text not null default 'AUD'",
    "link_insertion_notes": "text not null default ''",
    "price_band": "text not null default 'Ask'",
    "visibility_min_tier": "text not null default 'basic'",
    "is_listed": "integer not null default 0",
    "admin_review_status": "text not null default 'needs_review'",
    "last_seen_from_instantly": "text not null default ''",
}

PUBLISHER_ENTITY_EXTRA_COLUMNS = {
    "primary_domain": "text not null default ''",
    "group_key": "text not null default ''",
    "notes": "text not null default ''",
}

GENERIC_EMAIL_DOMAINS = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "me.com", "live.com"}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _email_domain(email: str) -> str:
    value = (email or "").strip().lower()
    if "@" not in value:
        return ""
    return value.rsplit("@", 1)[-1].strip()


def _title_from_domain(domain: str) -> str:
    stem = (domain or "").split(".")[0].replace("-", " ").replace("_", " ").strip()
    return " ".join(part.capitalize() for part in stem.split()) or domain or "Publisher"


def _entity_identity(row: sqlite3.Row) -> tuple[str, str, str, str]:
    contact_email = str(row["contact_email"] or "").strip().lower()
    contact_domain = _email_domain(contact_email)
    root_domain = str(row["root_domain"] or "").strip().lower()
    site_name = str(row["site_name"] or "").strip()
    if contact_email:
        group_key = f"email:{contact_email}"
        primary_domain = root_domain if contact_domain in GENERIC_EMAIL_DOMAINS else contact_domain
        name = site_name or _title_from_domain(root_domain) if contact_domain in GENERIC_EMAIL_DOMAINS else _title_from_domain(contact_domain)
        return group_key, name or "Publisher", contact_email, primary_domain
    if contact_domain:
        group_key = f"contact-domain:{contact_domain}"
        return group_key, _title_from_domain(contact_domain), "", contact_domain
    group_key = f"domain:{root_domain or row['id']}"
    return group_key, site_name or _title_from_domain(root_domain), "", root_domain


def assign_missing_publisher_entities(connection: sqlite3.Connection) -> None:
    now = _now_iso()
    rows = connection.execute(
        """
        select link_deals.*
        from link_deals
        left join publisher_entities on publisher_entities.id = link_deals.publisher_entity_id
        where link_deals.publisher_entity_id is null or publisher_entities.id is null
        order by link_deals.id
        """
    ).fetchall()
    for row in rows:
        group_key, name, primary_email, primary_domain = _entity_identity(row)
        entity = connection.execute("select id from publisher_entities where group_key=?", (group_key,)).fetchone()
        if entity:
            entity_id = entity["id"]
        else:
            cursor = connection.execute(
                """
                insert into publisher_entities (name, primary_email, primary_domain, group_key, notes, created_at, updated_at)
                values (?, ?, ?, ?, '', ?, ?)
                """,
                (name, primary_email, primary_domain, group_key, now, now),
            )
            entity_id = int(cursor.lastrowid)
        connection.execute("update link_deals set publisher_entity_id=? where id=?", (entity_id, row["id"]))


def refresh_generic_publisher_entity_labels(connection: sqlite3.Connection) -> None:
    now = _now_iso()
    rows = connection.execute(
        """
        select publisher_entities.id, publisher_entities.name, publisher_entities.primary_domain,
               link_deals.site_name, link_deals.root_domain
        from publisher_entities
        join link_deals on link_deals.publisher_entity_id=publisher_entities.id
        where publisher_entities.primary_email like '%@gmail.com'
           or publisher_entities.primary_email like '%@outlook.com'
           or publisher_entities.primary_email like '%@hotmail.com'
           or publisher_entities.primary_email like '%@yahoo.com'
           or publisher_entities.primary_email like '%@icloud.com'
           or publisher_entities.primary_email like '%@me.com'
           or publisher_entities.primary_email like '%@live.com'
        group by publisher_entities.id
        having count(link_deals.id)=1
        """
    ).fetchall()
    for row in rows:
        better_name = str(row["site_name"] or "").strip() or _title_from_domain(str(row["root_domain"] or ""))
        better_domain = str(row["root_domain"] or "").strip().lower()
        if better_name and row["name"] in {"Gmail", "Outlook", "Hotmail", "Yahoo", "Icloud", "Me", "Live"}:
            connection.execute(
                "update publisher_entities set name=?, primary_domain=?, updated_at=? where id=?",
                (better_name, better_domain or row["primary_domain"], now, row["id"]),
            )


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_dirs()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
        reply_sync_columns = {row["name"] for row in connection.execute("pragma table_info(instantly_reply_sync)").fetchall()}
        if "raw_json" not in reply_sync_columns:
            connection.execute("alter table instantly_reply_sync add column raw_json text not null default '{}'")
        existing_entities = {row["name"] for row in connection.execute("pragma table_info(publisher_entities)").fetchall()}
        for column, definition in PUBLISHER_ENTITY_EXTRA_COLUMNS.items():
            if column not in existing_entities:
                connection.execute(f"alter table publisher_entities add column {column} {definition}")
        existing = {row["name"] for row in connection.execute("pragma table_info(link_deals)").fetchall()}
        for column, definition in LINK_DEAL_EXTRA_COLUMNS.items():
            if column not in existing:
                connection.execute(f"alter table link_deals add column {column} {definition}")
        connection.execute("create unique index if not exists idx_publisher_entities_group_key on publisher_entities(group_key)")
        rows = connection.execute("select id, root_domain, tld from link_deals where tld='' or tld is null").fetchall()
        for row in rows:
            root_domain = row["root_domain"] or ""
            parts = root_domain.split(".")
            tld = ".".join(parts[-2:]) if len(parts) >= 3 and parts[-1] == "au" else (parts[-1] if len(parts) > 1 else "")
            connection.execute("update link_deals set tld=? where id=?", (tld, row["id"]))
        connection.execute("update link_deals set publisher_cost_amount=coalesce(publisher_cost_amount, price_amount)")
        connection.execute(
            """
            update link_deals
            set publisher_cost_currency=price_currency
            where publisher_cost_currency='' or publisher_cost_amount=price_amount
            """
        )
        connection.execute("update link_deals set last_seen_from_instantly=updated_at where last_seen_from_instantly=''")
        assign_missing_publisher_entities(connection)
        refresh_generic_publisher_entity_labels(connection)
        connection.commit()
