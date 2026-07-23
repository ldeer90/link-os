import { useCallback, useEffect, useState } from "react";
import { ArrowSquareOut, CaretLeft, CaretRight, CheckCircle, ClockCounterClockwise, Funnel, LinkSimple, ShieldCheck, UploadSimple, X } from "@phosphor-icons/react";
import { useSearchParams } from "react-router-dom";
import { apiRequest, listFrom, toQuery, totalFrom } from "../lib/api";
import { formatDate, formatNumber, titleCase } from "../lib/format";
import { DomainRecord } from "../types";
import { useApi } from "../hooks/useApi";
import {
  Button,
  EmptyState,
  ErrorState,
  PageHeader,
  PageSkeleton,
  SearchField,
  StatusBadge,
  TableShell,
  tableCellClass,
  tableHeaderClass,
} from "../components/ui";

const lifecycleFilters = ["", "imported", "queued", "scraping", "email_found", "no_email", "failed", "verified", "suppressed", "outreach_ready", "campaign_queued", "uploaded", "contacted", "replied", "offer_review", "offer_approved", "listed", "lost"];
const evidenceFilters = [
  ["", "All outreach evidence"],
  ["instantly_uploaded", "Uploaded to Instantly"],
  ["sent_confirmed", "Sent confirmed"],
  ["uploaded_no_send_record", "Uploaded — no send record"],
  ["replied", "Reply received"],
  ["offer_or_deal", "Offer or deal"],
  ["listed", "Listed"],
  ["engagement_confirmed", "Any confirmed engagement"],
  ["no_outreach_evidence", "No outreach recorded"],
] as const;
const historyFilters = [
  ["", "All historical milestones"],
  ["entered_system", "Entered LINK OS"],
  ["scrape_attempted", "Discovery processed"],
  ["contact_identity_captured", "Email captured"],
  ["public_contact_captured", "Strict public-page evidence"],
  ["verified_contact", "Strict verification evidence"],
  ["instantly_uploaded", "Uploaded to Instantly"],
  ["sent_confirmed", "Sent confirmed"],
  ["replied", "Reply or deal response"],
  ["offer_recorded", "Offer recorded"],
  ["listed", "Listed in catalogue"],
] as const;
const scopeFilters = [
  ["", "All canonical domains"],
  ["link_building", "Link-building scope"],
  ["unrelated_instantly_only", "Other Instantly campaigns only"],
] as const;
const pageSizeOptions = [25, 50, 100, 250, 500] as const;

export function DomainsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const status = searchParams.get("status") ?? "";
  const evidence = searchParams.get("evidence") ?? "";
  const history = searchParams.get("history") ?? "";
  const scope = searchParams.get("scope") ?? "";
  const requestedPageSize = Number(searchParams.get("page_size") ?? 100);
  const pageSize = pageSizeOptions.includes(requestedPageSize as (typeof pageSizeOptions)[number]) ? requestedPageSize : 100;
  const requestedPage = Number(searchParams.get("page") ?? 1);
  const page = Number.isFinite(requestedPage) && requestedPage > 0 ? Math.floor(requestedPage) - 1 : 0;
  const [selected, setSelected] = useState<DomainRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const query = toQuery({ search, status, evidence, history, scope, page_size: pageSize, offset: page * pageSize });
  const loader = useCallback(() => apiRequest<unknown>(`/domains${query}`), [query]);
  const resource = useApi(loader, [loader]);
  const domains = listFrom<DomainRecord>(resource.data, ["domains"]);
  const total = totalFrom(resource.data, domains);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const firstVisible = total ? page * pageSize + 1 : 0;
  const lastVisible = Math.min(page * pageSize + domains.length, total);
  const hasPrevious = page > 0;
  const hasNext = lastVisible < total;

  function resetPage(next: URLSearchParams) {
    next.delete("page");
  }

  function updatePage(nextPage: number) {
    const next = new URLSearchParams(searchParams);
    if (nextPage > 0) next.set("page", String(nextPage + 1));
    else next.delete("page");
    setSearchParams(next, { replace: true });
  }

  function updatePageSize(value: number) {
    const next = new URLSearchParams(searchParams);
    if (value === 100) next.delete("page_size");
    else next.set("page_size", String(value));
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  function updateSearch(value: string) {
    setSearch(value);
    const next = new URLSearchParams(searchParams);
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  function updateEvidence(value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("evidence", value);
    else next.delete("evidence");
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  function updateStatus(value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("status", value);
    else next.delete("status");
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  function updateHistory(value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("history", value);
    else next.delete("history");
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  function updateScope(value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("scope", value);
    else next.delete("scope");
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  function clearFilters() {
    setSearch("");
    const next = new URLSearchParams(searchParams);
    next.delete("status");
    next.delete("evidence");
    next.delete("history");
    next.delete("scope");
    resetPage(next);
    setSearchParams(next, { replace: true });
  }

  useEffect(() => {
    if (!selected || domains.some((domain) => String(domain.id) === String(selected.id))) return;
    setSelected(null);
  }, [domains, selected]);

  useEffect(() => {
    if (total > 0 && page >= totalPages) updatePage(totalPages - 1);
  // URL pagination is intentionally corrected only when the server total changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, total, totalPages]);

  async function openDomain(domain: DomainRecord) {
    setSelected(domain);
    setDetailLoading(true);
    try {
      const payload = await apiRequest<DomainRecord | { data: DomainRecord }>(`/domains/${domain.id}`);
      setSelected("data" in payload ? payload.data : payload);
    } catch {
      // The row still carries enough information to remain inspectable.
    } finally {
      setDetailLoading(false);
    }
  }

  return (
    <div className="space-y-7 animate-enter">
      <PageHeader eyebrow="Canonical registry" title="Domains" description="One normalized record per domain, with outreach evidence and duplicate-work protection visible." />

      <div className="grid gap-4 border-y border-slate-200 bg-white px-5 py-4 md:grid-cols-[auto_1fr] md:items-center">
        <ShieldCheck size={22} weight="regular" className="text-emerald-700" />
        <div>
          <p className="text-sm font-semibold text-zinc-950">Duplicate-work protection is always active</p>
          <p className="mt-1 max-w-[78ch] text-xs leading-5 text-slate-500">Every import and discovery source reuses the canonical domain record. Previous outreach, suppressions, replies, deals, listings and active reservations block automatic recontact. Only failed or no-email crawls may refresh after 90 days.</p>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_12rem_14rem_14rem_14rem_auto] xl:items-end">
        <label className="grid gap-2 text-xs font-medium text-slate-600">Search domains<SearchField value={search} onChange={(event) => updateSearch(event.target.value)} placeholder="Domain or email" aria-label="Search domains" /></label>
        <label className="grid gap-2 text-xs font-medium text-slate-600" htmlFor="domain-status">Lifecycle<select id="domain-status" aria-label="Lifecycle status" className="field" value={status} onChange={(event) => updateStatus(event.target.value)}>
          {lifecycleFilters.map((value) => <option value={value} key={value || "all"}>{value ? titleCase(value) : "All lifecycle stages"}</option>)}
        </select></label>
        <label className="grid gap-2 text-xs font-medium text-slate-600" htmlFor="domain-evidence">Outreach evidence<select id="domain-evidence" aria-label="Outreach evidence" className="field" value={evidence} onChange={(event) => updateEvidence(event.target.value)}>
          {evidenceFilters.map(([value, label]) => <option value={value} key={value || "all-evidence"}>{label}</option>)}
        </select></label>
        <label className="grid gap-2 text-xs font-medium text-slate-600" htmlFor="domain-history">Historical milestone<select id="domain-history" aria-label="Historical milestone" className="field" value={history} onChange={(event) => updateHistory(event.target.value)}>
          {historyFilters.map(([value, label]) => <option value={value} key={value || "all-history"}>{label}</option>)}
        </select></label>
        <label className="grid gap-2 text-xs font-medium text-slate-600" htmlFor="domain-scope">Domain scope<select id="domain-scope" aria-label="Domain scope" className="field" value={scope} onChange={(event) => updateScope(event.target.value)}>
          {scopeFilters.map(([value, label]) => <option value={value} key={value || "all-scope"}>{label}</option>)}
        </select></label>
        {(search || status || evidence || history || scope) && <Button variant="ghost" type="button" onClick={clearFilters} icon={<X size={16} weight="regular" />}>Clear</Button>}
      </div>

      <div className="flex flex-col gap-4 border-b border-slate-200 pb-4 sm:flex-row sm:items-end sm:justify-between">
        <span className="inline-flex items-center gap-2"><Funnel size={15} weight="regular" /> Server-side filters</span>
        <div className="flex flex-wrap items-end gap-4">
          <label className="grid gap-2 text-xs font-medium text-slate-600" htmlFor="domain-page-size">
            Rows per page
            <select id="domain-page-size" aria-label="Rows per page" className="field min-w-28" value={pageSize} onChange={(event) => updatePageSize(Number(event.target.value))}>
              {pageSizeOptions.map((value) => <option value={value} key={value}>{value}</option>)}
            </select>
          </label>
          <span className="pb-3 font-mono text-xs text-slate-500">{formatNumber(total)} records</span>
        </div>
      </div>

      {resource.loading && !resource.data ? (
        <PageSkeleton />
      ) : resource.error && !resource.data ? (
        <ErrorState error={resource.error} onRetry={resource.reload} title="Domains are unavailable" />
      ) : domains.length ? (
        <div className="space-y-4">
        <TableShell label="Domains">
          <thead><tr><th className={tableHeaderClass}>Domain</th><th className={tableHeaderClass}>Source</th><th className={tableHeaderClass}>Lifecycle</th><th className={tableHeaderClass}>Outreach evidence</th><th className={tableHeaderClass}>Best contact</th><th className={tableHeaderClass}>Last attempt</th></tr></thead>
          <tbody>
            {domains.map((domain) => (
              <tr className="cursor-pointer bg-white transition hover:bg-slate-50" key={domain.id} onClick={() => void openDomain(domain)}>
                <td className={tableCellClass}>
                  <button className="focus-ring text-left font-medium text-zinc-950 hover:text-emerald-800" type="button">{domain.registrable_domain ?? domain.domain}</button>
                  <p className="mt-1 font-mono text-[11px] uppercase tracking-[0.08em] text-slate-400">{domain.tld ?? "—"}{domain.country ? ` · ${domain.country}` : ""}</p>
                </td>
                <td className={`${tableCellClass} max-w-[18rem]`}>
                  <p className="flex items-center gap-2 text-xs font-medium text-zinc-950">
                    {domain.sources?.[0]?.type === "backlink_profile" ? <LinkSimple size={14} weight="regular" className="shrink-0 text-emerald-700" /> : <UploadSimple size={14} weight="regular" className="shrink-0 text-slate-500" />}
                    <span className="line-clamp-2">{domain.source_label ?? "Canonical historical record"}</span>
                  </p>
                  {(domain.source_count ?? 0) > 1 && <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.08em] text-slate-400">+{formatNumber((domain.source_count ?? 1) - 1)} more source{(domain.source_count ?? 1) === 2 ? "" : "s"}</p>}
                </td>
                <td className={tableCellClass}><StatusBadge status={domain.status} /></td>
                <td className={tableCellClass}><StatusBadge status={domain.outreach_evidence_status ?? "no_outreach_evidence"} label={domain.outreach_evidence_label} /><p className="mt-1 max-w-[32ch] text-xs text-slate-500">{domain.outreach_evidence_reason}</p></td>
                <td className={`${tableCellClass} font-mono text-xs`}>{domain.best_email ?? "—"}</td>
                <td className={tableCellClass}>{formatDate(domain.last_attempt_at, true)}</td>
              </tr>
            ))}
          </tbody>
        </TableShell>
        <nav className="flex flex-col gap-3 border-t border-slate-200 pt-4 sm:flex-row sm:items-center sm:justify-between" aria-label="Domain pages">
          <p className="font-mono text-xs text-slate-500">Showing {formatNumber(firstVisible)}–{formatNumber(lastVisible)} of {formatNumber(total)} matching domains · Page {formatNumber(page + 1)} of {formatNumber(totalPages)}</p>
          <div className="flex gap-2">
            <Button type="button" disabled={!hasPrevious} onClick={() => updatePage(Math.max(0, page - 1))} icon={<CaretLeft size={15} weight="regular" />}>Previous</Button>
            <Button type="button" disabled={!hasNext} onClick={() => updatePage(page + 1)} icon={<CaretRight size={15} weight="regular" />}>Next</Button>
          </div>
        </nav>
        </div>
      ) : (
        <EmptyState
          title={search || status || evidence || history || scope ? "No domains match these filters" : "No domains imported"}
          description={search || status || evidence || history || scope ? "Clear a filter or search a different registrable domain." : "Use Intake to queue the first durable domain import."}
          action={(search || status || evidence || history || scope) && <Button type="button" onClick={clearFilters}>Clear filters</Button>}
        />
      )}

      {selected && (
        <div className="fixed inset-0 flex justify-end bg-zinc-950/25 p-0 backdrop-blur-[2px] md:p-4" role="dialog" aria-modal="true" aria-label={`Evidence for ${selected.domain}`} onMouseDown={(event) => { if (event.currentTarget === event.target) setSelected(null); }}>
          <aside className="min-h-[100dvh] w-full overflow-y-auto bg-white p-5 shadow-2xl md:min-h-0 md:max-w-xl md:rounded-xl md:border md:border-slate-200 md:p-7">
            <div className="flex items-start justify-between gap-4 border-b border-slate-200 pb-5">
              <div>
                <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-emerald-700">Domain record</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight text-zinc-950">{selected.registrable_domain ?? selected.domain}</h2>
                <div className="mt-3"><StatusBadge status={selected.status} /></div>
              </div>
              <Button variant="ghost" type="button" onClick={() => setSelected(null)} aria-label="Close domain detail" icon={<X size={18} weight="regular" />} />
            </div>

            <dl className="grid grid-cols-2 gap-x-6 gap-y-5 border-b border-slate-200 py-6 text-sm">
              <div><dt className="text-xs text-slate-500">Best contact</dt><dd className="mt-1 break-all font-mono text-zinc-950">{selected.best_email ?? "Not selected"}</dd></div>
              <div><dt className="text-xs text-slate-500">Evidence records</dt><dd className="mt-1 font-mono text-zinc-950">{formatNumber(selected.evidence_count)}</dd></div>
              <div><dt className="text-xs text-slate-500">Last contact</dt><dd className="mt-1 text-zinc-950">{formatDate(selected.last_contacted_at, true)}</dd></div>
              <div><dt className="text-xs text-slate-500">Refresh eligible</dt><dd className="mt-1 text-zinc-950">{formatDate(selected.refresh_eligible_at)}</dd></div>
            </dl>

            <div className="border-b border-slate-200 py-6">
              <h3 className="flex items-center gap-2 font-semibold text-zinc-950"><ShieldCheck size={18} weight="regular" /> Outreach and duplicate protection</h3>
              <div className="mt-4 flex flex-wrap items-center gap-3"><StatusBadge status={selected.outreach_evidence_status ?? "no_outreach_evidence"} label={selected.outreach_evidence_label} /><span className="text-xs text-slate-500">{formatDate(selected.outreach_evidence_at, true)}</span></div>
              <p className="mt-3 text-sm leading-6 text-slate-600">{selected.outreach_evidence_reason}</p>
              <div className="mt-5 border-l-2 border-emerald-600 pl-4">
                <p className="flex items-center gap-2 text-sm font-semibold text-zinc-950"><CheckCircle size={16} weight="regular" className="text-emerald-700" /> One canonical domain record</p>
                <ul className="mt-2 space-y-2 text-xs leading-5 text-slate-600">
                  {(selected.duplicate_protection_reasons ?? []).map((reason) => <li key={reason}>{reason}</li>)}
                </ul>
              </div>
            </div>

            <div className="border-b border-slate-200 py-6">
              <h3 className="flex items-center gap-2 font-semibold text-zinc-950"><LinkSimple size={18} weight="regular" /> Discovery sources</h3>
              <p className="mt-2 text-xs leading-5 text-slate-500">Every retained backlink profile and canonical intake path is shown here. Repeated discoveries reuse this same domain record.</p>
              <ol className="mt-4 divide-y divide-slate-100 border-y border-slate-200">
                {(selected.sources ?? []).map((source) => (
                  <li className="py-4" key={`${source.type}:${source.id}`}>
                    <div className="flex items-start gap-3">
                      {source.type === "backlink_profile" ? <LinkSimple size={17} weight="regular" className="mt-0.5 shrink-0 text-emerald-700" /> : <UploadSimple size={17} weight="regular" className="mt-0.5 shrink-0 text-slate-500" />}
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-zinc-950">{source.label}</p>
                        {source.detail && <p className="mt-1 text-xs leading-5 text-slate-500">{source.detail}</p>}
                        <div className="mt-2 flex flex-wrap items-center gap-3">
                          {source.discovered_at && <span className="font-mono text-[10px] text-slate-400">{formatDate(source.discovered_at, true)}</span>}
                          {source.analysis_id && <a className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700 hover:text-emerald-800" href={`/ops/backlinks?analysis=${source.analysis_id}`}>Open analysis <ArrowSquareOut size={12} weight="regular" /></a>}
                        </div>
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            </div>

            <div className="py-6">
              <h3 className="flex items-center gap-2 font-semibold text-zinc-950"><ClockCounterClockwise size={18} weight="regular" /> Evidence timeline</h3>
              {detailLoading ? (
                <p className="mt-5 text-sm text-slate-500">Loading evidence…</p>
              ) : selected.events?.length ? (
                <ol className="mt-5 space-y-0">
                  {selected.events.map((event, index) => (
                    <li className="relative border-l border-slate-200 pb-6 pl-5 last:pb-0" key={event.id ?? index}>
                      <span className="absolute -left-1 top-1 size-2 rounded-full bg-emerald-600 ring-4 ring-white" />
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="text-sm font-medium text-zinc-950">{titleCase(event.event_type ?? event.status)}</p>
                        <time className="font-mono text-[11px] text-slate-500">{formatDate(event.created_at, true)}</time>
                      </div>
                      {event.detail && <p className="mt-1 text-sm leading-6 text-slate-600">{event.detail}</p>}
                      {event.evidence_url && <a className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-emerald-700 hover:text-emerald-800" href={event.evidence_url} target="_blank" rel="noreferrer">Open public evidence <ArrowSquareOut size={13} weight="regular" /></a>}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="mt-4 text-sm leading-6 text-slate-600">No evidence events have been attached to this domain yet.</p>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
