from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import DB_PATH, ensure_project_dirs


SCHEMA = """
create table if not exists guest_post_search_results (
    id integer primary key autoincrement,
    search_engine text not null,
    source_query text not null,
    result_url text not null,
    root_domain text not null,
    title text not null default '',
    snippet text not null default '',
    matched_phrase text not null default '',
    raw_cache_path text not null default '',
    fetched_at text not null,
    unique(search_engine, source_query, result_url)
);

create index if not exists idx_search_results_domain on guest_post_search_results(root_domain);
create index if not exists idx_search_results_query on guest_post_search_results(source_query);

create table if not exists guest_post_prospects (
    root_domain text primary key,
    site_name text not null default '',
    url_found text not null default '',
    source_url text not null default '',
    matched_phrase text not null default '',
    opportunity_type text not null default '',
    niche text not null default '',
    country text not null default 'AU',
    domain_type text not null default '',
    contact_email text not null default '',
    contact_page text not null default '',
    has_media_kit integer not null default 0,
    has_write_for_us_page integer not null default 0,
    has_sponsored_post_language integer not null default 0,
    estimated_quality text not null default '',
    quality_tier text not null default '',
    spam_risk text not null default '',
    why_relevant text not null default '',
    reject_reason text not null default '',
    notes text not null default '',
    source_query text not null default '',
    scraped_at text not null,
    updated_at text not null
);

create index if not exists idx_prospects_quality on guest_post_prospects(quality_tier);
create index if not exists idx_prospects_opportunity on guest_post_prospects(opportunity_type);

create table if not exists guest_post_contact_emails (
    root_domain text not null,
    email text not null,
    email_type text not null default '',
    email_source_url text not null default '',
    email_source_method text not null default 'page_scrape',
    source_page_type text not null default '',
    confidence integer not null default 0,
    discovered_at text not null,
    primary key (root_domain, email, email_source_method)
);

create index if not exists idx_contact_emails_domain on guest_post_contact_emails(root_domain);
create index if not exists idx_contact_emails_email on guest_post_contact_emails(email);

create table if not exists guest_post_crawl_pages (
    root_domain text not null,
    url text not null,
    final_url text not null default '',
    status text not null default '',
    title text not null default '',
    matched_phrases text not null default '',
    opportunity_type text not null default '',
    has_media_kit integer not null default 0,
    has_write_for_us_page integer not null default 0,
    has_sponsored_post_language integer not null default 0,
    text_excerpt text not null default '',
    error_message text not null default '',
    crawled_at text not null,
    primary key (root_domain, url)
);

create index if not exists idx_crawl_pages_domain on guest_post_crawl_pages(root_domain);

create table if not exists guest_post_suppression (
    root_domain text primary key,
    reason text not null default '',
    added_at text not null
);

create table if not exists guest_post_campaign_drafts (
    id text primary key,
    name text not null,
    status text not null default 'draft',
    campaign_group text not null default '',
    sequence_json text not null default '[]',
    payload_json text not null default '{}',
    created_at text not null,
    updated_at text not null
);

create table if not exists link_deals (
    id integer primary key autoincrement,
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
    created_at text not null,
    updated_at text not null
);

create index if not exists idx_link_deals_status on link_deals(deal_status);
create index if not exists idx_link_deals_domain on link_deals(root_domain);
create unique index if not exists idx_link_deals_thread_contact on link_deals(instantly_thread_id, contact_email);

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
    processed_at text not null,
    unique(email_id)
);

create index if not exists idx_reply_sync_campaign on instantly_reply_sync(campaign_id);
create index if not exists idx_reply_sync_lead on instantly_reply_sync(lead_email);
create index if not exists idx_reply_sync_classification on instantly_reply_sync(classification);

create table if not exists discovery_runs (
    id text primary key,
    source_type text not null,
    status text not null default 'running',
    parameters_json text not null default '{}',
    started_at text not null,
    finished_at text not null default '',
    notes text not null default ''
);

create index if not exists idx_discovery_runs_source on discovery_runs(source_type);
create index if not exists idx_discovery_runs_status on discovery_runs(status);

create table if not exists discovery_sources (
    id integer primary key autoincrement,
    discovery_run_id text not null,
    source_type text not null,
    source_key text not null,
    source_query text not null default '',
    fetched_at text not null,
    status text not null default '',
    result_count integer not null default 0,
    cache_path text not null default '',
    error_message text not null default '',
    unique(discovery_run_id, source_type, source_key)
);

create index if not exists idx_discovery_sources_run on discovery_sources(discovery_run_id);
create index if not exists idx_discovery_sources_type on discovery_sources(source_type);

create table if not exists candidate_urls (
    url text primary key,
    root_domain text not null,
    source_type text not null,
    source_query text not null default '',
    source_run_id text not null default '',
    matched_phrase text not null default '',
    title text not null default '',
    discovered_at text not null,
    last_seen_at text not null,
    status text not null default 'new',
    evidence_score integer not null default 0
);

create index if not exists idx_candidate_urls_domain on candidate_urls(root_domain);
create index if not exists idx_candidate_urls_source on candidate_urls(source_type);
create index if not exists idx_candidate_urls_status on candidate_urls(status);

create table if not exists candidate_domains (
    root_domain text primary key,
    first_source_type text not null default '',
    first_source_query text not null default '',
    first_seen_at text not null,
    last_seen_at text not null,
    candidate_url_count integer not null default 0,
    confirmed_evidence_count integer not null default 0,
    best_evidence_url text not null default '',
    matched_phrase text not null default '',
    opportunity_type text not null default '',
    niche text not null default '',
    quality_tier text not null default 'Unconfirmed',
    contact_email text not null default '',
    email_type text not null default '',
    why_relevant text not null default '',
    reject_reason text not null default '',
    updated_at text not null
);

create index if not exists idx_candidate_domains_quality on candidate_domains(quality_tier);
create index if not exists idx_candidate_domains_opportunity on candidate_domains(opportunity_type);

create table if not exists crawl_queue (
    url text primary key,
    root_domain text not null,
    reason text not null default '',
    priority integer not null default 50,
    status text not null default 'pending',
    attempts integer not null default 0,
    last_error text not null default '',
    created_at text not null,
    updated_at text not null
);

create index if not exists idx_crawl_queue_status_priority on crawl_queue(status, priority);
create index if not exists idx_crawl_queue_domain on crawl_queue(root_domain);

create table if not exists crawl_results (
    url text primary key,
    root_domain text not null,
    final_url text not null default '',
    status text not null default '',
    title text not null default '',
    text_excerpt text not null default '',
    matched_phrase text not null default '',
    opportunity_type text not null default '',
    niche text not null default '',
    outbound_links_json text not null default '[]',
    error_message text not null default '',
    crawled_at text not null
);

create index if not exists idx_crawl_results_domain on crawl_results(root_domain);
create index if not exists idx_crawl_results_match on crawl_results(matched_phrase);

create table if not exists domain_evidence (
    id integer primary key autoincrement,
    root_domain text not null,
    evidence_url text not null,
    source_type text not null,
    source_query text not null default '',
    matched_phrase text not null default '',
    opportunity_type text not null default '',
    confidence_score integer not null default 0,
    crawl_timestamp text not null,
    unique(root_domain, evidence_url, matched_phrase, source_type)
);

create index if not exists idx_domain_evidence_domain on domain_evidence(root_domain);
create index if not exists idx_domain_evidence_phrase on domain_evidence(matched_phrase);

create table if not exists coverage_metrics (
    metric_key text primary key,
    metric_value text not null,
    calculated_at text not null
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_project_dirs()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
        connection.commit()
