import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  ArrowSquareOut,
  CheckCircle,
  Coins,
  FunnelSimple,
  MagnifyingGlass,
  ShieldCheck,
  TreeStructure,
  WarningCircle,
  X,
  XCircle,
} from "@phosphor-icons/react";
import { Link } from "react-router-dom";
import { Button, EmptyState, ErrorState, InlineNotice, Metric, PageHeader, SearchField, StatusBadge, TableShell, tableCellClass, tableHeaderClass } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom, toQuery } from "../lib/api";
import { formatDate, formatNumber, titleCase } from "../lib/format";
import { BacklinkAnalysis, BacklinkCandidate, BacklinkEstimate } from "../types";

type Mode = "competitor_prospecting" | "client_profile_audit";

const activeStatuses = new Set(["queued", "fetching", "filtering", "queueing_scrape"]);

export function BacklinksPage() {
  const [mode, setMode] = useState<Mode>("competitor_prospecting");
  const [clientDomain, setClientDomain] = useState("");
  const [competitorText, setCompetitorText] = useState("");
  const [creditCap, setCreditCap] = useState(500);
  const [authorityFloor, setAuthorityFloor] = useState(20);
  const [authorityCeiling, setAuthorityCeiling] = useState<number | "">(80);
  const [sourceUrlFilter, setSourceUrlFilter] = useState("");
  const [estimate, setEstimate] = useState<BacklinkEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [candidateStatus, setCandidateStatus] = useState("");
  const [candidateSearch, setCandidateSearch] = useState("");
  const [candidateCompetitor, setCandidateCompetitor] = useState("");
  const [candidateLinkType, setCandidateLinkType] = useState("");
  const [candidateAnchorIntent, setCandidateAnchorIntent] = useState("");
  const [candidateMinAuthority, setCandidateMinAuthority] = useState("");
  const [candidateEmailStatus, setCandidateEmailStatus] = useState("");
  const [candidateCrawlStatus, setCandidateCrawlStatus] = useState("");
  const [reviewing, setReviewing] = useState<string | null>(null);

  const runsLoader = useCallback(() => apiRequest<unknown>("/backlink-analyses?limit=50"), []);
  const runsResource = useApi(runsLoader, [runsLoader]);
  const runs = listFrom<BacklinkAnalysis>(runsResource.data);
  const selectedRun = runs.find((run) => run.id === selectedRunId) ?? runs[0];
  const healthLoader = useCallback(() => apiRequest<Record<string, unknown>>("/integrations/seranking/health"), []);
  const healthResource = useApi(healthLoader, [healthLoader]);
  const candidateQuery = toQuery({
    status: candidateStatus,
    search: candidateSearch,
    competitor: candidateCompetitor,
    link_type: candidateLinkType,
    anchor_intent: candidateAnchorIntent,
    min_authority: candidateMinAuthority || undefined,
    email_status: candidateEmailStatus,
    crawl_status: candidateCrawlStatus,
    limit: 100,
  });
  const candidateLoader = useCallback(
    () => selectedRun ? apiRequest<unknown>(`/backlink-analyses/${selectedRun.id}/candidates${candidateQuery}`) : Promise.resolve({ items: [] }),
    [selectedRun?.id, candidateQuery],
  );
  const candidatesResource = useApi(candidateLoader, [candidateLoader]);
  const candidates = listFrom<BacklinkCandidate>(candidatesResource.data);

  useEffect(() => {
    if (!selectedRunId && runs.length) setSelectedRunId(runs[0].id);
  }, [runs, selectedRunId]);

  useEffect(() => {
    if (candidateCompetitor && !selectedRun?.competitor_domains.includes(candidateCompetitor)) {
      setCandidateCompetitor("");
    }
  }, [candidateCompetitor, selectedRun?.id, selectedRun?.competitor_domains]);

  useEffect(() => {
    if (!selectedRun || !activeStatuses.has(selectedRun.status)) return;
    const interval = window.setInterval(() => {
      void runsResource.reload();
      void candidatesResource.reload();
    }, 2_000);
    return () => window.clearInterval(interval);
  }, [selectedRun?.id, selectedRun?.status, runsResource.reload, candidatesResource.reload]);

  const competitors = useMemo(() => competitorText.split(/[\n,\s]+/).map((value) => value.trim()).filter(Boolean).slice(0, 5), [competitorText]);

  async function requestEstimate(event: FormEvent) {
    event.preventDefault();
    setEstimating(true);
    setEstimate(null);
    setActionError(null);
    try {
      const result = await apiRequest<BacklinkEstimate>("/backlink-analyses/estimate", {
        method: "POST",
        body: JSON.stringify({ mode, client_domain: clientDomain, competitor_domains: competitors, credit_cap: creditCap, authority_floor: authorityFloor, authority_ceiling: authorityCeiling === "" ? null : authorityCeiling, source_url_filter: sourceUrlFilter || null }),
      });
      setEstimate(result);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "The estimate could not be created.");
    } finally {
      setEstimating(false);
    }
  }

  async function confirmAndCreate() {
    if (!estimate) return;
    setCreating(true);
    setActionError(null);
    try {
      const created = await apiRequest<{ id: string }>("/backlink-analyses", {
        method: "POST",
        body: JSON.stringify({
          mode: estimate.mode,
          client_domain: estimate.client_domain,
          competitor_domains: estimate.competitor_domains,
          credit_cap: estimate.credit_cap,
          confirmed_credit_cap: estimate.credit_cap,
          authority_floor: estimate.authority_floor,
          authority_ceiling: estimate.authority_ceiling,
          source_url_filter: estimate.source_url_filter,
        }),
      });
      setSelectedRunId(created.id);
      setEstimate(null);
      await runsResource.reload();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "The analysis could not be queued.");
    } finally {
      setCreating(false);
    }
  }

  async function decide(candidate: BacklinkCandidate, decision: "approved" | "rejected" | "manual_review") {
    if (!selectedRun) return;
    setReviewing(candidate.id);
    setActionError(null);
    try {
      const confidence = decision === "approved" ? 0.9 : decision === "rejected" ? 0.85 : 0.5;
      const reason = decision === "approved" ? "operator_editorial_relevance" : decision === "rejected" ? "operator_not_a_link_prospect" : "operator_requires_more_evidence";
      await apiRequest(`/backlink-analyses/${selectedRun.id}/reviews`, {
        method: "POST",
        body: JSON.stringify({ decisions: [{
          candidate_id: candidate.id,
          decision,
          confidence,
          reason_codes: [reason],
          summary: `Operator reviewed the public backlink evidence for ${candidate.domain} in LINK OS.`,
        }] }),
      });
      await Promise.all([candidatesResource.reload(), runsResource.reload()]);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "The decision could not be recorded.");
    } finally {
      setReviewing(null);
    }
  }

  async function runAction(action: "retry" | "cancel") {
    if (!selectedRun) return;
    setActionError(null);
    try {
      await apiRequest(`/backlink-analyses/${selectedRun.id}/${action}`, { method: "POST", body: JSON.stringify({}) });
      await runsResource.reload();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : `The run could not be ${action === "retry" ? "retried" : "cancelled"}.`);
    }
  }

  const counters = selectedRun?.counters ?? {};
  const count = (key: string) => typeof counters[key] === "number" ? counters[key] as number : 0;
  const providerBalance = typeof healthResource.data?.balance === "number" ? healthResource.data.balance : null;
  const monthlyUsage = typeof healthResource.data?.monthly_usage === "number" ? healthResource.data.monthly_usage : 0;
  const monthlyCap = typeof healthResource.data?.monthly_cap === "number" ? healthResource.data.monthly_cap : 10_000;
  const candidateCount = candidatesResource.data && typeof candidatesResource.data === "object" && "count" in candidatesResource.data && typeof candidatesResource.data.count === "number" ? candidatesResource.data.count : candidates.length;
  const activeCandidateFilters = [candidateSearch, candidateStatus, candidateCompetitor, candidateLinkType, candidateAnchorIntent, candidateMinAuthority, candidateEmailStatus, candidateCrawlStatus].filter(Boolean).length;

  function clearCandidateFilters() {
    setCandidateSearch("");
    setCandidateStatus("");
    setCandidateCompetitor("");
    setCandidateLinkType("");
    setCandidateAnchorIntent("");
    setCandidateMinAuthority("");
    setCandidateEmailStatus("");
    setCandidateCrawlStatus("");
  }

  return (
    <div className="space-y-8 animate-enter">
      <PageHeader
        eyebrow="Credit-safe prospect mining"
        title="Backlink discovery"
        description="Mine competitor referring domains or audit a client profile through one canonical, review-gated path. No candidate reaches scraping until it is approved and re-enters normal LINK OS intake."
        actions={<Button type="button" onClick={() => { void runsResource.reload(); void healthResource.reload(); }} icon={<ArrowClockwise size={16} />}>Refresh</Button>}
      />

      <section className="grid gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 shadow-panel sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="SE Ranking balance" value={providerBalance === null ? "—" : formatNumber(providerBalance)} detail="Zero-credit health readback" />
        <Metric label="Monthly usage" value={`${formatNumber(monthlyUsage)} / ${formatNumber(monthlyCap)}`} detail="LINK OS enforced cap" />
        <Metric label="Pending review" value={formatNumber(count("pending_review"))} detail="Awaiting Codex or operator" tone={count("pending_review") ? "neutral" : "good"} />
        <Metric label="Scrape queued" value={formatNumber(count("scrape_queued"))} detail="Via canonical imports only" tone="good" />
      </section>

      <section className="grid gap-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(360px,.85fr)]">
        <form className="rounded-xl border border-slate-200 bg-white p-5 shadow-panel md:p-6" onSubmit={requestEstimate}>
          <div className="flex items-start gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-emerald-50 text-emerald-700"><TreeStructure size={21} /></span>
            <div><h2 className="text-lg font-semibold tracking-tight text-zinc-950">New analysis</h2><p className="mt-1 text-sm leading-6 text-slate-600">Estimate first. LINK OS reserves two credits per target to measure the complete referring-domain profile, then spends the remainder on authority-ranked evidence.</p></div>
          </div>
          <div className="mt-6 grid gap-5 md:grid-cols-2">
            <label className="block"><span className="text-sm font-medium text-zinc-900">Mode</span><select className="field mt-2" value={mode} onChange={(event) => { setMode(event.target.value as Mode); setEstimate(null); }}><option value="competitor_prospecting">Competitor prospecting</option><option value="client_profile_audit">Client profile audit</option></select></label>
            <label className="block"><span className="text-sm font-medium text-zinc-900">Client domain</span><input className="field mt-2" placeholder="client.com.au" required value={clientDomain} onChange={(event) => { setClientDomain(event.target.value); setEstimate(null); }} /></label>
          </div>
          {mode === "competitor_prospecting" && <label className="mt-5 block"><span className="text-sm font-medium text-zinc-900">Competitors <span className="font-normal text-slate-500">· up to five</span></span><textarea className="field mt-2 min-h-28 resize-y" placeholder={"competitor-one.com\ncompetitor-two.com"} required value={competitorText} onChange={(event) => { setCompetitorText(event.target.value); setEstimate(null); }} /><span className="mt-1.5 block text-xs text-slate-500">{competitors.length} of 5 targets supplied. Credits are divided evenly.</span></label>}
          <div className="mt-5 grid gap-5 sm:grid-cols-3">
            <label className="block"><span className="text-sm font-medium text-zinc-900">Maximum credits</span><input className="field mt-2" type="number" min={1} max={2500} value={creditCap} onChange={(event) => { setCreditCap(Number(event.target.value)); setEstimate(null); }} /><span className="mt-1.5 block text-xs text-slate-500">Default 500 · hard maximum 2,500</span></label>
            <label className="block"><span className="text-sm font-medium text-zinc-900">Minimum Domain InLink Rank</span><input className="field mt-2" type="number" min={0} max={100} value={authorityFloor} onChange={(event) => { setAuthorityFloor(Number(event.target.value)); setEstimate(null); }} /><span className="mt-1.5 block text-xs text-slate-500">Higher thresholds preserve credits.</span></label>
            <label className="block"><span className="text-sm font-medium text-zinc-900">Maximum Domain InLink Rank</span><input className="field mt-2" type="number" min={authorityFloor} max={100} placeholder="80" value={authorityCeiling} onChange={(event) => { setAuthorityCeiling(event.target.value === "" ? "" : Number(event.target.value)); setEstimate(null); }} /><span className="mt-1.5 block text-xs text-slate-500">Default 80 excludes very high-authority sites that rarely sell placements. Set 100 only when deliberately auditing them.</span></label>
          </div>
          <label className="mt-5 block"><span className="text-sm font-medium text-zinc-900">Referring URL filter <span className="font-normal text-slate-500">· optional</span></span><input className="field mt-2" placeholder=".com.au" value={sourceUrlFilter} onChange={(event) => { setSourceUrlFilter(event.target.value); setEstimate(null); }} /><span className="mt-1.5 block text-xs text-slate-500">Targets referring page URLs containing this text. Leave blank for global discovery.</span></label>
          <div className="mt-6 flex items-center justify-between gap-4 border-t border-slate-200 pt-5"><p className="text-xs leading-5 text-slate-500">The estimate may check subscription balance, which costs zero credits.</p><Button type="submit" variant="primary" disabled={estimating} icon={<MagnifyingGlass size={16} />}>{estimating ? "Estimating…" : "Estimate credits"}</Button></div>
        </form>

        <div className="space-y-4">
          {estimate ? <div className="rounded-xl border border-slate-200 bg-zinc-950 p-6 text-white shadow-panel">
            <div className="flex items-center justify-between gap-4"><span className="grid size-10 place-items-center rounded-lg bg-white/10 text-emerald-300"><Coins size={21} /></span><StatusBadge status={estimate.allowed ? "healthy" : "error"} label={estimate.allowed ? "Preflight passed" : "Blocked"} /></div>
            <p className="mt-6 font-mono text-4xl font-medium tracking-tight">{formatNumber(estimate.maximum_paid_records)}</p><p className="mt-1 text-sm text-slate-300">maximum referring domains retrieved</p>
            <dl className="mt-6 grid grid-cols-2 gap-4 border-y border-white/10 py-5 text-sm"><div><dt className="text-slate-400">Confirmed ceiling</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.credit_cap)}</dd></div><div><dt className="text-slate-400">Expected max spend</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.predicted_credits)}</dd></div><div><dt className="text-slate-400">Profile-size count</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.count_credit_cost)} credits</dd></div><div><dt className="text-slate-400">Provider balance</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.balance)}</dd></div><div><dt className="text-slate-400">Monthly remaining</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.monthly_remaining)}</dd></div><div><dt className="text-slate-400">Cached targets</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.targets.filter((target) => target.cache_hit).length)}</dd></div><div><dt className="text-slate-400">Authority range</dt><dd className="mt-1 font-mono text-white">{formatNumber(estimate.authority_floor)}–{estimate.authority_ceiling ?? 100}</dd></div><div><dt className="text-slate-400">Referring URL filter</dt><dd className="mt-1 font-mono text-white">{estimate.source_url_filter || "All URLs"}</dd></div></dl>
            {estimate.blocking_reasons.length > 0 && <p className="mt-4 text-sm text-rose-300">{estimate.blocking_reasons.map(titleCase).join(" · ")}</p>}
            <Button className="mt-6 w-full" type="button" variant="primary" disabled={!estimate.allowed || creating} onClick={() => void confirmAndCreate()} icon={<ShieldCheck size={17} />}>{creating ? "Queueing…" : `Confirm maximum ${formatNumber(estimate.credit_cap)} credits & queue`}</Button>
          </div> : <EmptyState title="Estimate before spending" description="Matching results are reused for 30 days. LINK OS checks known exclusions locally before candidates reach review." />}
          <InlineNotice tone="info" title={mode === "client_profile_audit" ? "Audit-only safety" : "Review-gated prospecting"}>{mode === "client_profile_audit" ? "Client profile results are retained for analysis and can never automatically create scrape jobs." : "Plausible domains wait for Codex or operator review. Approval requires positive editorial relevance and confidence of at least 0.75."}</InlineNotice>
        </div>
      </section>

      {actionError && <InlineNotice tone="danger" title="Action could not be completed">{actionError}</InlineNotice>}

      <section>
        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-end md:justify-between"><div><h2 className="text-lg font-semibold tracking-tight text-zinc-950">Analysis runs</h2><p className="mt-1 text-sm text-slate-600">Provider progress, cache state and errors update live while a run is active.</p></div>{runs.length > 0 && <select className="field min-w-80" value={selectedRun?.id ?? ""} onChange={(event) => setSelectedRunId(event.target.value)}>{runs.map((run) => <option key={run.id} value={run.id}>{run.client_domain} · {titleCase(run.mode)} · {titleCase(run.status)}</option>)}</select>}</div>
        {runsResource.error && !runsResource.data ? <ErrorState error={runsResource.error} onRetry={runsResource.reload} /> : !selectedRun ? <EmptyState title="No backlink analyses yet" description="Use the estimator above to create the first canonical run." /> : <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
          <div className="grid gap-5 border-b border-slate-200 p-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"><div><div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold text-zinc-950">{selectedRun.client_domain}</h3><StatusBadge status={selectedRun.status} /></div><p className="mt-2 text-sm text-slate-600">{titleCase(selectedRun.mode)} · {formatNumber(selectedRun.actual_credits)} actual / {formatNumber(selectedRun.credit_cap)} confirmed credits · started {formatDate(selectedRun.created_at, true)}</p>{selectedRun.error_detail && <p className="mt-2 text-sm text-rose-700">{selectedRun.error_code ? `${titleCase(selectedRun.error_code)}: ` : ""}{selectedRun.error_detail}</p>}</div><div className="flex gap-2">{["failed", "partial"].includes(selectedRun.status) && <Button type="button" onClick={() => void runAction("retry")} icon={<ArrowClockwise size={15} />}>Retry</Button>}{!(["complete", "cancelled", "failed"].includes(selectedRun.status)) && <Button type="button" variant="danger" onClick={() => void runAction("cancel")} icon={<XCircle size={15} />}>Cancel</Button>}</div></div>
          <div className="border-b border-slate-200 bg-slate-50 px-5 py-3 text-xs leading-5 text-slate-600">Coverage compares captured authority-filtered evidence with the competitor's complete raw referring-domain count, including low-quality domains below the selected authority floor.</div>
          <div className="grid gap-px bg-slate-200 sm:grid-cols-2 lg:grid-cols-4">{[["Total ref. domains", formatNumber(count("total_referring_domains"))], ["Captured", formatNumber(count("returned"))], ["Profile coverage", `${count("coverage_percent").toFixed(1)}%`], ["Excluded", formatNumber(count("excluded"))], ["Pending", formatNumber(count("pending_review"))], ["Approved", formatNumber(count("approved"))], ["Imported", formatNumber(count("imported"))], ["Cached targets", formatNumber(count("cached"))]].map(([label, value]) => <div className="bg-white px-4 py-4" key={label}><p className="text-[11px] font-medium uppercase tracking-[.1em] text-slate-500">{label}</p><p className="mt-2 font-mono text-xl font-medium text-zinc-950">{value}</p></div>)}</div>
        </div>}
      </section>

      {selectedRun && <section>
        <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-end md:justify-between"><div><h2 className="text-lg font-semibold tracking-tight text-zinc-950">Candidate review</h2><p className="mt-1 text-sm text-slate-600">Public backlink evidence stays attached to its competitor and resulting canonical domain.</p></div><p className="font-mono text-xs text-slate-500">{formatNumber(candidateCount)} matching · up to 100 shown</p></div>
        <div className="mb-5 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
          <div className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
            <label className="block sm:col-span-2"><span className="text-xs font-medium text-zinc-900">Source, page, anchor or target</span><SearchField className="mt-2" aria-label="Search backlink evidence" placeholder="Search backlink evidence" value={candidateSearch} onChange={(event) => setCandidateSearch(event.target.value)} /></label>
            <label className="block"><span className="text-xs font-medium text-zinc-900">Competitor</span><select aria-label="Filter by competitor" className="field mt-2" value={candidateCompetitor} onChange={(event) => setCandidateCompetitor(event.target.value)}><option value="">All competitors</option>{selectedRun.competitor_domains.map((domain) => <option key={domain} value={domain}>{domain}</option>)}</select></label>
            <label className="block"><span className="text-xs font-medium text-zinc-900">Decision</span><select aria-label="Filter by decision" className="field mt-2" value={candidateStatus} onChange={(event) => setCandidateStatus(event.target.value)}><option value="">All decisions</option><option value="awaiting_codex_review">Awaiting Codex review</option><option value="manual_review">Manual review</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="hard_rejected">Hard excluded</option><option value="audit_only">Audit only</option><option value="import_queued">Import queued</option><option value="imported">Imported</option></select></label>
            <label className="block"><span className="text-xs font-medium text-zinc-900">Link type</span><select aria-label="Filter by link type" className="field mt-2" value={candidateLinkType} onChange={(event) => setCandidateLinkType(event.target.value)}><option value="">Follow and nofollow</option><option value="follow">Follow evidence</option><option value="nofollow">Nofollow evidence</option></select></label>
            <label className="block"><span className="text-xs font-medium text-zinc-900">Anchor intent</span><select aria-label="Filter by anchor intent" className="field mt-2" value={candidateAnchorIntent} onChange={(event) => setCandidateAnchorIntent(event.target.value)}><option value="">Any anchor</option><option value="commercial">Any commercial signal</option><option value="strong_commercial">Strong commercial</option><option value="likely_commercial">Likely commercial</option><option value="brand">Brand</option><option value="editorial">Editorial</option><option value="generic">Generic or URL</option><option value="empty">No text anchor</option><option value="unknown">Other or unknown</option></select></label>
            <label className="block"><span className="text-xs font-medium text-zinc-900">Minimum authority</span><input aria-label="Minimum authority" className="field mt-2" type="number" min={0} max={100} placeholder="Any" value={candidateMinAuthority} onChange={(event) => setCandidateMinAuthority(event.target.value)} /></label>
            <label className="block"><span className="text-xs font-medium text-zinc-900">Public email</span><select aria-label="Filter by public email" className="field mt-2" value={candidateEmailStatus} onChange={(event) => setCandidateEmailStatus(event.target.value)}><option value="">Any email state</option><option value="found">Email found</option><option value="not_found">No public email</option><option value="not_canonical">Not imported</option></select></label>
            <label className="block sm:col-span-2 lg:col-span-1"><span className="text-xs font-medium text-zinc-900">Crawl status</span><select aria-label="Filter by crawl status" className="field mt-2" value={candidateCrawlStatus} onChange={(event) => setCandidateCrawlStatus(event.target.value)}><option value="">Any crawl status</option><option value="scraping">Scraping</option><option value="email_found">Email found</option><option value="no_email">No email</option><option value="failed">Failed</option><option value="verified">Verified</option><option value="outreach_ready">Outreach ready</option></select></label>
          </div>
          <div className="flex flex-col gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><p className="flex items-center gap-2 text-xs text-slate-600"><FunnelSimple size={15} className="text-emerald-700" />{activeCandidateFilters ? `${activeCandidateFilters} active filter${activeCandidateFilters === 1 ? "" : "s"}` : "Showing the complete candidate set for this run"}</p>{activeCandidateFilters > 0 && <Button type="button" variant="ghost" onClick={clearCandidateFilters} icon={<X size={14} />}>Clear filters</Button>}</div>
        </div>
        {candidatesResource.error && !candidatesResource.data ? <ErrorState error={candidatesResource.error} onRetry={candidatesResource.reload} title="Candidates are unavailable" /> : candidates.length === 0 ? <EmptyState title="No candidates in this view" description="Change the filter or wait for the provider and local filtering stages to finish." /> : <TableShell label="Backlink candidates"><thead><tr><th className={tableHeaderClass}>Candidate</th><th className={tableHeaderClass}>Authority</th><th className={tableHeaderClass}>Evidence</th><th className={tableHeaderClass}>Decision</th><th className={tableHeaderClass}>Actions</th></tr></thead><tbody>{candidates.map((candidate) => <tr className="bg-white" key={candidate.id}>
          <td className={tableCellClass}><div className="flex items-start gap-2">{candidate.status === "approved" || candidate.status === "imported" ? <CheckCircle className="mt-0.5 shrink-0 text-emerald-600" size={17} weight="fill" /> : candidate.status.includes("reject") ? <XCircle className="mt-0.5 shrink-0 text-rose-600" size={17} weight="fill" /> : <WarningCircle className="mt-0.5 shrink-0 text-amber-600" size={17} weight="fill" />}<div><p className="font-medium text-zinc-950">{candidate.domain}</p><div className="mt-2 flex flex-wrap gap-1.5"><StatusBadge status={candidate.status} />{candidate.crawl_status && <StatusBadge status={candidate.crawl_status} />}</div>{candidate.existing_domain_id && <p className="mt-2 font-mono text-[11px] text-slate-500">{formatNumber(candidate.public_email_count ?? 0)} public email{candidate.public_email_count === 1 ? "" : "s"}</p>}{candidate.existing_domain_id && <Link className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-emerald-700 hover:underline" to={`/domains?search=${encodeURIComponent(candidate.domain)}`}>Open canonical domain <ArrowSquareOut size={12} /></Link>}</div></div></td>
          <td className={`${tableCellClass} table-number`}><p className="font-mono text-lg text-zinc-950">{formatNumber(candidate.highest_domain_inlink_rank)}</p><p className="mt-1 text-xs text-slate-500">{candidate.has_dofollow ? "Includes follow link" : "No follow evidence"} · {formatNumber(candidate.occurrence_count)} competitor{candidate.occurrence_count === 1 ? "" : "s"}</p><p className={`mt-2 inline-flex rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[.06em] ${candidate.commercial_anchor_class === "commercial" ? "bg-emerald-100 text-emerald-800" : candidate.commercial_anchor_class === "commercial_likely" ? "bg-amber-100 text-amber-800" : "bg-slate-100 text-slate-600"}`}>{titleCase(candidate.commercial_anchor_class || "unknown")} · {Math.round((candidate.commercial_anchor_score || 0) * 100)}%</p>{candidate.competitor_domains && candidate.competitor_domains.length > 0 && <p className="mt-2 max-w-44 truncate text-xs text-slate-500" title={candidate.competitor_domains.join(", ")}>{candidate.competitor_domains.join(", ")}</p>}</td>
          <td className={`${tableCellClass} min-w-80`}><details><summary className="cursor-pointer text-sm font-medium text-emerald-700">{formatNumber(candidate.evidence.length)} evidence record{candidate.evidence.length === 1 ? "" : "s"}</summary><div className="mt-3 space-y-3">{candidate.evidence.map((evidence) => <div className="rounded-lg border border-slate-200 bg-slate-50 p-3" key={evidence.id}><p className="mb-1 font-mono text-[10px] uppercase tracking-[.08em] text-slate-400">Competitor · {evidence.competitor_domain || "Unknown"}</p><a className="inline-flex max-w-full items-center gap-1 truncate text-xs font-medium text-zinc-900 hover:text-emerald-700" href={evidence.source_url} target="_blank" rel="noreferrer">{evidence.page_title || evidence.source_url}<ArrowSquareOut size={12} /></a><p className="mt-1 line-clamp-2 text-xs text-slate-500">Anchor: {evidence.anchor_text || "—"} · Target: {evidence.target_url || "—"}</p><p className="mt-1 text-[11px] font-medium text-slate-600">{titleCase(evidence.anchor_class || "unknown")} intent · {Math.round((evidence.anchor_commercial_score || 0) * 100)}%</p></div>)}</div></details></td>
          <td className={`${tableCellClass} max-w-xs`}><p className="text-xs leading-5 text-slate-600">{candidate.decision_summary || candidate.reason_codes.map(titleCase).join(" · ")}</p>{candidate.codex_confidence != null && <p className="mt-2 font-mono text-[11px] text-slate-400">Confidence {Math.round(candidate.codex_confidence * 100)}%</p>}</td>
          <td className={tableCellClass}>{["awaiting_codex_review", "manual_review", "rejected"].includes(candidate.status) ? <div className="flex flex-wrap gap-2"><Button type="button" variant="primary" disabled={reviewing === candidate.id} onClick={() => void decide(candidate, "approved")}>Approve</Button><Button type="button" disabled={reviewing === candidate.id} onClick={() => void decide(candidate, "manual_review")}>Hold</Button><Button type="button" variant="danger" disabled={reviewing === candidate.id} onClick={() => void decide(candidate, "rejected")}>Reject</Button></div> : candidate.import_id ? <Link className="text-xs font-medium text-emerald-700 hover:underline" to="/intake">View canonical import</Link> : <span className="text-xs text-slate-400">Recorded</span>}</td>
        </tr>)}</tbody></TableShell>}
      </section>}
    </div>
  );
}
