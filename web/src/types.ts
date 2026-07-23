export type LifecycleStatus =
  | "imported"
  | "queued"
  | "scraping"
  | "email_found"
  | "no_email"
  | "failed"
  | "verified"
  | "suppressed"
  | "outreach_ready"
  | "campaign_queued"
  | "uploaded"
  | "contacted"
  | "replied"
  | "offer_approved"
  | "review"
  | "listed"
  | "lost";

export interface PipelineStage {
  status: LifecycleStatus | string;
  count: number;
}

export interface OutreachEvidenceSummary {
  scope: "link_building" | "link_building_com.au" | string;
  total_domains: number;
  historical_memberships: number;
  contact_confirmed: number;
  membership_only: number;
  instantly_uploaded?: number;
  sent_confirmed?: number;
  uploaded_no_send_record?: number;
  evidence_backed_engaged: number;
  reply_domains: number;
  offer_domains: number;
  listed_domains: number;
  protected_domains: number;
  workspace_instantly_uploaded?: number;
  excluded_unrelated_campaign_domains?: number;
  link_building_campaigns?: number;
  workspace_campaigns?: number;
  tiers: Record<string, number>;
}

export interface OverviewData {
  pipeline?: PipelineStage[] | Record<string, number>;
  historical_funnel?: Record<string, number>;
  current_state?: Record<string, number>;
  outreach_evidence?: OutreachEvidenceSummary;
  australian_outreach_evidence?: OutreachEvidenceSummary;
  totals?: Record<string, number>;
  queue?: {
    pending?: number;
    active?: number;
    completed_last_hour?: number;
    failed_last_hour?: number;
    velocity_per_hour?: number;
  };
  senders?: SenderHealth[];
  recent_replies?: Reply[];
  failures?: Job[];
}

export interface ImportRecord {
  id: string | number;
  filename?: string | null;
  status: string;
  total_rows?: number;
  processed_rows?: number;
  accepted?: number;
  duplicates?: number;
  invalid?: number;
  suppressed?: number;
  previously_contacted?: number;
  refresh_eligible?: number;
  created_at?: string;
}

export interface DomainRecord {
  id: string | number;
  domain: string;
  registrable_domain?: string;
  status: LifecycleStatus | string;
  tld?: string;
  country?: string | null;
  best_email?: string | null;
  last_attempt_at?: string | null;
  last_contacted_at?: string | null;
  refresh_eligible_at?: string | null;
  evidence_count?: number;
  suppression_reason?: string | null;
  outreach_evidence_status?: string;
  outreach_evidence_label?: string;
  outreach_evidence_reason?: string;
  outreach_evidence_at?: string | null;
  canonical_dedupe?: boolean;
  automatic_recontact_blocked?: boolean;
  duplicate_protection_reasons?: string[];
  source_label?: string;
  source_count?: number;
  sources?: DomainSource[];
  events?: DomainEvent[];
}

export interface DomainSource {
  id: string;
  type: string;
  label: string;
  detail?: string | null;
  analysis_id?: string | null;
  import_id?: string | null;
  client_domain?: string | null;
  competitor_domains?: string[];
  source_url_filter?: string | null;
  authority_floor?: number | null;
  authority_ceiling?: number | null;
  filename?: string | null;
  discovered_at?: string | null;
}

export interface DomainEvent {
  id?: string | number;
  event_type?: string;
  status?: string;
  detail?: string;
  evidence_url?: string;
  created_at?: string;
}

export interface Job {
  id: string | number;
  job_type?: string;
  status: string;
  domain?: string;
  attempts?: number;
  max_attempts?: number;
  run_after?: string | null;
  leased_until?: string | null;
  last_error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ScrapeActivity {
  id: string;
  domain_id: string;
  domain: string;
  status: string;
  attempt_number: number;
  started_at: string;
  completed_at?: string | null;
  elapsed_seconds: number;
  pages_requested: number;
  pages_crawled: number;
  emails_found: number;
  error_code?: string | null;
  error_detail?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  job_attempts?: number | null;
  job_max_attempts?: number | null;
  leased_until?: string | null;
  current_url?: string | null;
  last_event?: string | null;
  last_event_at?: string | null;
  page_status?: string | null;
  max_pages?: number | null;
}

export interface ScrapeLiveEvent {
  id: string;
  event_type: "scrape_started" | "page_started" | "page_crawled" | "email_found" | "scrape_failed" | "scrape_completed" | string;
  attempt_id?: string | null;
  domain_id?: string | null;
  domain?: string | null;
  job_id?: string | null;
  current_url?: string | null;
  page_status?: string | null;
  status?: string | null;
  status_code?: number | null;
  pages_attempted?: number;
  pages_crawled?: number;
  emails_found?: number;
  max_pages?: number;
  error_code?: string | null;
  occurred_at?: string;
}

export interface SenderHealth {
  id?: string;
  email: string;
  status: string;
  healthy?: boolean;
  daily_limit?: number;
  sent_today?: number;
  bounce_rate?: number;
  unsubscribe_rate?: number;
  warmup_enabled?: boolean;
  blocking_reason?: string | null;
}

export interface Campaign {
  id: string | number;
  name: string;
  status: string;
  managed?: boolean;
  member_count?: number;
  uploaded_count?: number;
  contacted_count?: number;
  reply_count?: number;
  bounce_rate?: number;
  unsubscribe_rate?: number;
  sender_email?: string | null;
  blocking_reasons?: string[];
  batch_number?: number;
  launch_id?: string | null;
  segment?: "au_publishers" | "travel_global" | string | null;
  hard_bounce_count?: number;
  hard_bounce_limit?: number;
  unsubscribe_count?: number;
}

export interface Reply {
  id: string | number;
  domain?: string;
  from_email?: string;
  subject?: string;
  snippet?: string;
  received_at?: string;
  review_status?: string;
  campaign_name?: string;
  has_attachments?: boolean;
}

export interface Offer {
  id: string | number;
  reply_id?: string | number;
  domain: string;
  publisher_email?: string;
  placement_type?: string | null;
  original_amount?: number | null;
  original_currency?: string | null;
  reseller_price_aud?: number | null;
  confidence?: number | null;
  status: string;
  pricing_rule_version?: string;
  evidence?: string;
  private?: boolean;
  listed?: boolean;
}

export interface Listing {
  id: string | number;
  domain: string;
  status: string;
  visibility?: string;
  reseller_price_aud?: number | null;
  placement_type?: string;
  publisher_entity?: string;
  enquiries?: number;
  updated_at?: string;
}

export interface Agency {
  id: string | number;
  name: string;
  email?: string;
  visibility_tier?: string;
  status?: string;
  enquiry_count?: number;
  last_active_at?: string;
}

export interface IntegrationState {
  name: string;
  status: string;
  message?: string;
  checked_at?: string;
  writable?: boolean;
  balance?: number | null;
}

export interface BacklinkEvidence {
  id: string;
  source_url: string;
  competitor_domain?: string | null;
  target_url?: string | null;
  page_title?: string | null;
  anchor_text?: string | null;
  anchor_class?: string;
  anchor_commercial_score?: number;
  anchor_signals?: string[];
  nofollow?: boolean | null;
  inlink_rank?: number | null;
  domain_inlink_rank?: number | null;
  first_seen?: string | null;
  last_visited?: string | null;
}

export interface BacklinkCandidate {
  id: string;
  domain: string;
  normalized_domain: string;
  status: string;
  occurrence_count: number;
  highest_domain_inlink_rank?: number | null;
  has_dofollow: boolean;
  local_score: number;
  commercial_anchor_class: string;
  commercial_anchor_score: number;
  commercial_anchor_signals: string[];
  codex_confidence?: number | null;
  reason_codes: string[];
  decision_summary?: string | null;
  import_id?: string | null;
  existing_domain_id?: string | null;
  competitor_domains?: string[];
  crawl_status?: string | null;
  public_email_count?: number;
  evidence: BacklinkEvidence[];
}

export interface BacklinkAnalysis {
  id: string;
  mode: "competitor_prospecting" | "client_profile_audit";
  status: string;
  client_domain: string;
  competitor_domains: string[];
  source_url_filter?: string | null;
  credit_cap: number;
  estimated_credits: number;
  actual_credits: number;
  balance_before?: number | null;
  balance_after?: number | null;
  authority_floor: number;
  authority_ceiling?: number | null;
  counters: Record<string, number | boolean>;
  error_code?: string | null;
  error_detail?: string | null;
  import_id?: string | null;
  created_at: string;
  completed_at?: string | null;
  targets?: Array<Record<string, unknown>>;
}

export interface BacklinkEstimate {
  mode: "competitor_prospecting" | "client_profile_audit";
  client_domain: string;
  competitor_domains: string[];
  source_url_filter?: string | null;
  credit_cap: number;
  predicted_credits: number;
  count_credit_cost: number;
  maximum_paid_records: number;
  authority_floor: number;
  authority_ceiling?: number | null;
  monthly_usage: number;
  monthly_cap: number;
  monthly_remaining: number;
  balance?: number | null;
  allowed: boolean;
  blocking_reasons: string[];
  confirmation_required: boolean;
  targets: Array<{ domain: string; role: string; credit_cap: number; count_credit_cost: number; retrieval_credit_cap: number; cache_hit: boolean }>;
}

export interface SystemHealth {
  status?: string;
  database?: string | IntegrationState;
  worker?: string | IntegrationState;
  integrations?: IntegrationState[] | Record<string, IntegrationState | string>;
  outreach_paused?: boolean;
  pause_reason?: string | null;
  blocking_reasons?: string[];
  senders?: SenderHealth[];
  shadow_mode?: boolean;
  checked_at?: string;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  page?: number;
  page_size?: number;
}
