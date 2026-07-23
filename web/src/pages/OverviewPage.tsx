import { ArrowRight, CaretRight, ChatCircleDots, Warning } from "@phosphor-icons/react";
import { Link } from "react-router-dom";
import { useCallback, useState } from "react";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom } from "../lib/api";
import { formatDate, formatNumber, titleCase } from "../lib/format";
import { Job, OverviewData, PipelineStage, Reply, SenderHealth } from "../types";
import {
  Button,
  EmptyState,
  ErrorState,
  Metric,
  PageHeader,
  PageSkeleton,
  SectionHeader,
  StatusBadge,
} from "../components/ui";

function pipelineFrom(data: OverviewData): PipelineStage[] {
  if (Array.isArray(data.pipeline)) return data.pipeline;
  if (data.pipeline && typeof data.pipeline === "object") {
    return Object.entries(data.pipeline).map(([status, count]) => ({ status, count: Number(count) || 0 }));
  }
  return [];
}

function sendersFrom(data: OverviewData): SenderHealth[] {
  return listFrom<SenderHealth>(data.senders, ["senders"]);
}

function repliesFrom(data: OverviewData): Reply[] {
  return listFrom<Reply>(data.recent_replies, ["replies"]);
}

function failuresFrom(data: OverviewData): Job[] {
  return listFrom<Job>(data.failures, ["jobs"]);
}

type FunnelStage = {
  key: string;
  label: string;
  phase: string;
  description: string;
  count: number;
  to: string;
};

function historicalFunnel(counts: Record<string, number>): FunnelStage[] {
  const stage = (key: string, label: string, phase: string, description: string): FunnelStage => ({
    key,
    label,
    phase,
    description,
    count: counts[key] ?? 0,
    to: `/domains?history=${key}`,
  });
  return [
    stage("entered_system", "Entered LINK OS", "Canonical intake", "Every deduplicated domain, regardless of source"),
    stage("scrape_attempted", "Discovery processed", "Discovery", "Crawled in LINK OS or processed by a historical link-building workflow"),
    stage("contact_identity_captured", "Email captured", "Contact discovery", "An email was captured by LINK OS or a historical link-building workflow"),
    stage("instantly_uploaded", "Uploaded to Instantly", "Outreach", "Lead exists in a scoped Instantly campaign"),
    stage("sent_confirmed", "Sent confirmed", "Outreach", "Instantly records delivery or engagement activity"),
    stage("replied", "Reply or deal response", "Deal flow", "A reply, offer or later publisher outcome is recorded"),
    stage("offer_recorded", "Offer recorded", "Deal flow", "Publisher pricing or placement terms are retained"),
    stage("listed", "Listed in catalogue", "Inventory", "Domain is available in the agency catalogue"),
  ];
}

function currentStateFunnel(counts: Record<string, number>): FunnelStage[] {
  const stage = (status: string, label: string, phase: string, description: string): FunnelStage => ({
    key: status,
    label,
    phase,
    description,
    count: counts[status] ?? 0,
    to: `/domains?scope=link_building&status=${status}`,
  });
  return [
    stage("imported", "Imported — awaiting next step", "Intake", "Currently accepted but not yet queued"),
    stage("queued", "Queued for scraping", "Discovery", "Waiting for a bounded crawl worker"),
    stage("scraping", "Scraping", "Discovery", "A worker is currently inspecting public pages"),
    stage("email_found", "Email found", "Discovery", "At least one public email has evidence"),
    stage("no_email", "No email found", "Discovery outcome", "Crawl completed without a usable public email"),
    stage("failed", "Scrape failed", "Discovery outcome", "Crawl exhausted its current attempts"),
    stage("verified", "Verified", "Qualification", "Contact passed the verification gate"),
    stage("suppressed", "Suppressed", "Qualification outcome", "Domain or contact is blocked from outreach"),
    stage("outreach_ready", "Outreach ready", "Outreach", "Eligible contact is ready for reservation"),
    stage("campaign_queued", "Campaign queued", "Outreach", "Contact is reserved for a campaign batch"),
    stage("uploaded", "Uploaded", "Outreach", "Currently recorded as uploaded by the canonical campaign workflow"),
    stage("contacted", "Contacted — stored lifecycle", "Outreach", "Current legacy-aware lifecycle value; use Historical reach for confirmed sends"),
    stage("replied", "Replied", "Deal flow", "Currently awaiting or retaining reply-stage processing"),
    stage("offer_review", "Offer review", "Deal flow", "Pricing or placement terms need review"),
    stage("offer_approved", "Offer approved", "Deal flow", "Publisher terms have been approved"),
    stage("listed", "Listed in catalogue", "Inventory", "Currently listed in the agency catalogue"),
    stage("lost", "Lost or rejected", "Closed", "Opportunity is closed and protected from recontact"),
  ];
}

export function OverviewPage() {
  const [funnelMode, setFunnelMode] = useState<"history" | "current">("history");
  const loader = useCallback(() => apiRequest<OverviewData>("/overview"), []);
  const resource = useApi(loader, [loader]);

  if (resource.loading && !resource.data) return <PageSkeleton />;
  if (resource.error && !resource.data) {
    return (
      <div className="space-y-6">
        <PageHeader eyebrow="Control plane" title="Overview" description="One view of intake, scraping, outreach safety, replies and live inventory." />
        <ErrorState error={resource.error} onRetry={resource.reload} title="The operations snapshot is unavailable" />
      </div>
    );
  }

  const data = resource.data ?? {};
  const pipeline = pipelineFrom(data);
  const senders = sendersFrom(data);
  const replies = repliesFrom(data);
  const failures = failuresFrom(data);
  const totals = data.totals ?? {};
  const verified = totals.verified ?? pipeline.find((stage) => stage.status === "verified")?.count ?? 0;
  const domains = totals.link_building_domains ?? totals.domains ?? pipeline.reduce((sum, stage) => sum + stage.count, 0);
  const openReplies = totals.open_replies ?? replies.filter((reply) => reply.review_status !== "resolved").length;
  const healthySenders = senders.filter((sender) => sender.healthy || sender.status === "healthy").length;
  const evidence = data.outreach_evidence;
  const australianEvidence = data.australian_outreach_evidence;
  const currentState = data.current_state ?? Object.fromEntries(pipeline.map((stage) => [stage.status, stage.count]));
  const historicalCounts = data.historical_funnel ?? {
    entered_system: domains,
    instantly_uploaded: evidence?.instantly_uploaded ?? evidence?.historical_memberships ?? 0,
    sent_confirmed: evidence?.sent_confirmed ?? evidence?.contact_confirmed ?? 0,
    replied: evidence?.reply_domains ?? 0,
    offer_recorded: evidence?.offer_domains ?? 0,
    listed: evidence?.listed_domains ?? 0,
  };
  const funnel = funnelMode === "history" ? historicalFunnel(historicalCounts) : currentStateFunnel(currentState);

  return (
    <div className="space-y-9 animate-enter">
      <PageHeader
        eyebrow="Control plane"
        title="Outreach, without blind spots."
        description="Track every domain from first import to private catalogue listing, with sending held behind health and evidence gates."
        actions={
          <Link to="/intake">
            <Button variant="primary" icon={<ArrowRight size={16} weight="regular" />}>Import domains</Button>
          </Link>
        }
      />

      <section className="grid gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 shadow-panel sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Link-building domains" value={formatNumber(domains)} detail="Canonical records in LINK OS scope" />
        <Metric label="Verified contacts" value={formatNumber(verified)} detail="Evidence-backed and safe to reserve" tone="good" />
        <Metric label="Replies to review" value={formatNumber(openReplies)} detail="No automatic publisher replies" />
        <Metric
          label="Healthy senders"
          value={`${healthySenders}/${senders.length}`}
          detail={senders.length ? "Required before campaign activation" : "Sender health not reported"}
          tone={healthySenders > 0 ? "good" : "bad"}
        />
      </section>

      {evidence ? (
        <section>
          <SectionHeader
            title="Outreach evidence"
            description="Guest-post and link-building campaigns only. Unrelated SEO audit and referral campaigns remain outside these funnel totals."
          />
          <div className="grid gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 shadow-panel sm:grid-cols-2 xl:grid-cols-4">
            <Link className="focus-ring block transition hover:-translate-y-px hover:bg-slate-50 active:translate-y-0" to="/domains?evidence=instantly_uploaded" aria-label="View all Instantly leads uploaded"><Metric label="Instantly leads uploaded" value={formatNumber(evidence.instantly_uploaded ?? evidence.historical_memberships)} detail="Added to an Instantly campaign" /></Link>
            <Link className="focus-ring block transition hover:-translate-y-px hover:bg-slate-50 active:translate-y-0" to="/domains?evidence=sent_confirmed" aria-label="View all sent-confirmed domains"><Metric label="Sent confirmed" value={formatNumber(evidence.sent_confirmed ?? evidence.contact_confirmed)} detail="Send, completion, bounce, open, click or reply recorded" tone="good" /></Link>
            <Link className="focus-ring block transition hover:-translate-y-px hover:bg-slate-50 active:translate-y-0" to="/domains?evidence=uploaded_no_send_record" aria-label="View uploaded domains with no send record"><Metric label="Uploaded — no send record" value={formatNumber(evidence.uploaded_no_send_record ?? evidence.membership_only)} detail="No delivery activity recorded by Instantly" /></Link>
            <Link className="focus-ring block transition hover:-translate-y-px hover:bg-slate-50 active:translate-y-0" to="/domains?evidence=engagement_confirmed" aria-label="View domains with confirmed engagement"><Metric label="Engagement confirmed" value={formatNumber(evidence.evidence_backed_engaged)} detail="Sent, reply, offer or listing evidence" tone="good" /></Link>
          </div>
          <div className="mt-px flex flex-wrap items-center divide-x divide-slate-200 border-x border-b border-slate-200 bg-white text-sm">
            <Link className="focus-ring px-4 py-3 font-medium text-slate-600 transition hover:bg-slate-50 hover:text-emerald-800" to="/domains?evidence=replied">{formatNumber(evidence.reply_domains)} reply domains</Link>
            <Link className="focus-ring px-4 py-3 font-medium text-slate-600 transition hover:bg-slate-50 hover:text-emerald-800" to="/domains?evidence=offer_or_deal">{formatNumber(evidence.offer_domains)} offer or deal domains</Link>
            <Link className="focus-ring px-4 py-3 font-medium text-slate-600 transition hover:bg-slate-50 hover:text-emerald-800" to="/domains?evidence=listed">{formatNumber(evidence.listed_domains)} listed domains</Link>
          </div>
          <div className="mt-3 grid gap-3 border-y border-slate-200 bg-white px-5 py-4 sm:grid-cols-[1fr_auto_auto] sm:items-center sm:gap-8">
            <div>
              <p className="text-sm font-semibold text-zinc-950">Instantly campaign scope</p>
              <p className="mt-1 text-xs leading-5 text-slate-500">Workspace memberships remain available for duplicate-contact safety, but only guest-post/link-building campaigns affect LINK OS totals.</p>
            </div>
            <div><p className="table-number text-lg font-semibold text-emerald-700">{formatNumber(evidence.link_building_campaigns)}</p><p className="text-xs text-slate-500">of {formatNumber(evidence.workspace_campaigns)} campaigns with leads included</p></div>
            <div><p className="table-number text-lg font-semibold text-zinc-950">{formatNumber(evidence.excluded_unrelated_campaign_domains)}</p><p className="text-xs text-slate-500">unrelated-only domains excluded</p></div>
          </div>
          {australianEvidence ? (
            <div className="mt-3 grid gap-3 rounded-xl border border-slate-200 bg-white px-5 py-4 shadow-panel sm:grid-cols-[1fr_repeat(4,auto)] sm:items-center sm:gap-8">
              <div>
                <p className="text-sm font-semibold text-zinc-950">Australian universe</p>
                <p className="mt-1 text-xs leading-5 text-slate-500">Strict domains ending in .com.au, using the same evidence rules.</p>
              </div>
              <div><p className="table-number text-lg font-semibold text-zinc-950">{formatNumber(australianEvidence.total_domains)}</p><p className="text-xs text-slate-500">known</p></div>
              <div><p className="table-number text-lg font-semibold text-zinc-950">{formatNumber(australianEvidence.instantly_uploaded ?? australianEvidence.historical_memberships)}</p><p className="text-xs text-slate-500">uploaded</p></div>
              <div><p className="table-number text-lg font-semibold text-emerald-700">{formatNumber(australianEvidence.sent_confirmed ?? australianEvidence.contact_confirmed)}</p><p className="text-xs text-slate-500">sent confirmed</p></div>
              <div><p className="table-number text-lg font-semibold text-amber-700">{formatNumber(australianEvidence.uploaded_no_send_record ?? australianEvidence.membership_only)}</p><p className="text-xs text-slate-500">no send record</p></div>
            </div>
          ) : null}
        </section>
      ) : null}

      <section>
        <div>
          <div className="mb-4 flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-zinc-950">Full domain funnel</h2>
              <p className="mt-1 max-w-[72ch] text-sm leading-6 text-slate-600">
                {funnelMode === "history"
                  ? "Historical reach shows every domain with durable evidence of reaching a milestone. Legacy imports can retain a later milestone without earlier crawl evidence."
                  : "Current state assigns each domain to one lifecycle value. These counts are mutually exclusive and describe what needs attention now."}
              </p>
            </div>
            <div className="inline-grid w-full grid-cols-2 rounded-lg border border-slate-200 bg-slate-100 p-1 md:w-auto" role="group" aria-label="Funnel view">
              <button type="button" aria-pressed={funnelMode === "history"} onClick={() => setFunnelMode("history")} className={`focus-ring rounded-md px-4 py-2 text-sm font-medium transition active:translate-y-px ${funnelMode === "history" ? "bg-white text-zinc-950 shadow-sm" : "text-slate-500 hover:text-zinc-950"}`}>Historical reach</button>
              <button type="button" aria-pressed={funnelMode === "current"} onClick={() => setFunnelMode("current")} className={`focus-ring rounded-md px-4 py-2 text-sm font-medium transition active:translate-y-px ${funnelMode === "current" ? "bg-white text-zinc-950 shadow-sm" : "text-slate-500 hover:text-zinc-950"}`}>Current state</button>
            </div>
          </div>
          {funnel.length ? (
            <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
              {funnel.map((stage, index) => {
                const max = Math.max(...funnel.map((item) => item.count), 1);
                return (
                  <Link
                    key={stage.key}
                    to={stage.to}
                    aria-label={`View ${stage.label} domains`}
                    className="focus-ring group grid grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-x-3 border-b border-slate-100 px-4 py-4 transition duration-200 hover:bg-slate-50 active:translate-y-px last:border-0 sm:grid-cols-[2.5rem_minmax(13rem,0.9fr)_minmax(10rem,1.4fr)_auto] sm:gap-x-5 sm:px-6"
                  >
                    <span className="font-mono text-xs text-slate-400">{String(index + 1).padStart(2, "0")}</span>
                    <span className="min-w-0">
                      <span className="block font-mono text-[10px] uppercase tracking-[0.12em] text-emerald-700">{stage.phase}</span>
                      <span className="mt-0.5 block text-sm font-semibold text-zinc-950">{stage.label}</span>
                      <span className="mt-1 block text-xs leading-5 text-slate-500 sm:hidden">{stage.description}</span>
                    </span>
                    <span className="col-span-2 col-start-2 mt-3 hidden min-w-0 sm:block sm:col-span-1 sm:col-start-auto sm:mt-0">
                      <span className="block text-xs leading-5 text-slate-500">{stage.description}</span>
                      <span className="mt-2 block h-1.5 overflow-hidden rounded-full bg-slate-100">
                        <span className="block h-full min-w-px origin-left rounded-full bg-emerald-600 transition-transform duration-300" style={{ transform: `scaleX(${stage.count / max})` }} />
                      </span>
                    </span>
                    <span className="flex min-w-20 items-center justify-end gap-2">
                      <span className="table-number text-right text-base font-semibold text-zinc-950">{formatNumber(stage.count)}</span>
                      <CaretRight size={15} weight="bold" className="text-slate-300 transition-transform duration-200 group-hover:translate-x-0.5 group-hover:text-emerald-700" />
                    </span>
                  </Link>
                );
              })}
            </div>
          ) : (
            <EmptyState title="No lifecycle data yet" description="Import domains to create durable records and queue bounded scrape jobs." />
          )}
        </div>

        <div className="mt-9">
          <SectionHeader title="Queue pulse" description="Worker throughput and current pressure." />
          <div className="rounded-xl border border-slate-200 bg-zinc-950 p-6 text-white shadow-panel">
            <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-emerald-300">Last 60 minutes</p>
            <div className="mt-7 grid grid-cols-2 gap-x-6 gap-y-7">
              <div>
                <p className="font-mono text-3xl font-medium">{formatNumber(data.queue?.completed_last_hour ?? data.queue?.velocity_per_hour)}</p>
                <p className="mt-1 text-xs text-zinc-400">jobs completed</p>
              </div>
              <div>
                <p className="font-mono text-3xl font-medium">{formatNumber(data.queue?.pending)}</p>
                <p className="mt-1 text-xs text-zinc-400">waiting</p>
              </div>
              <div>
                <p className="font-mono text-3xl font-medium text-emerald-300">{formatNumber(data.queue?.active)}</p>
                <p className="mt-1 text-xs text-zinc-400">leased now</p>
              </div>
              <div>
                <p className={`font-mono text-3xl font-medium ${(data.queue?.failed_last_hour ?? 0) > 0 ? "text-rose-300" : "text-zinc-200"}`}>
                  {formatNumber(data.queue?.failed_last_hour)}
                </p>
                <p className="mt-1 text-xs text-zinc-400">failed</p>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-8 xl:grid-cols-[1.25fr_0.75fr]">
        <div>
          <SectionHeader
            title="Recent replies"
            description="Inbound evidence awaiting extraction or approval."
            action={<Link className="text-sm font-medium text-emerald-700 hover:text-emerald-800" to="/replies">Open inbox</Link>}
          />
          {replies.length ? (
            <div className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
              {replies.slice(0, 5).map((reply) => (
                <div className="grid gap-3 px-4 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start" key={reply.id}>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <ChatCircleDots size={17} weight="regular" className="shrink-0 text-slate-400" />
                      <p className="truncate text-sm font-semibold text-zinc-950">{reply.domain ?? reply.from_email ?? "Unknown publisher"}</p>
                    </div>
                    <p className="mt-1 truncate text-sm text-slate-600">{reply.subject ?? reply.snippet ?? "Reply received"}</p>
                  </div>
                  <div className="flex items-center gap-3 sm:flex-col sm:items-end sm:gap-1">
                    <StatusBadge status={reply.review_status ?? "review"} />
                    <span className="text-xs text-slate-500">{formatDate(reply.received_at, true)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState title="No recent replies" description="The ten-minute reply sync will place new messages here without sending an automatic response." />
          )}
        </div>

        <div>
          <SectionHeader title="Exceptions" description="Worker failures that need attention." />
          {failures.length ? (
            <div className="space-y-2">
              {failures.slice(0, 5).map((job) => (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3" key={job.id}>
                  <div className="flex items-start gap-2">
                    <Warning size={17} weight="regular" className="mt-0.5 shrink-0 text-rose-700" />
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-rose-950">{job.domain ?? titleCase(job.job_type)}</p>
                      <p className="mt-1 line-clamp-2 text-xs leading-5 text-rose-800">{job.last_error ?? "Job exhausted its current attempt."}</p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState title="No active exceptions" description="Retryable jobs and hard failures will be separated here." />
          )}
        </div>
      </section>
    </div>
  );
}
