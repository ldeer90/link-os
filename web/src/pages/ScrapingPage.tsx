import { useCallback, useEffect, useState } from "react";
import { ArrowClockwise, CaretLeft, CaretRight, CheckCircle, Clock, EnvelopeSimple, GlobeSimple, HardDrives, Pulse, WarningCircle } from "@phosphor-icons/react";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom, toQuery, totalFrom } from "../lib/api";
import { formatDate, formatNumber, titleCase } from "../lib/format";
import { Job, ScrapeActivity, ScrapeLiveEvent } from "../types";
import {
  Button,
  EmptyState,
  ErrorState,
  Metric,
  PageHeader,
  PageSkeleton,
  StatusBadge,
  TableShell,
  tableCellClass,
  tableHeaderClass,
} from "../components/ui";

export function ScrapingPage() {
  const pageSize = 100;
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const [retrying, setRetrying] = useState<string | number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const query = toQuery({ job_type: "scrape", status, page_size: pageSize, offset: page * pageSize });
  const loader = useCallback(() => apiRequest<unknown>(`/jobs${query}`), [query]);
  const resource = useApi(loader, [loader]);
  const activityLoader = useCallback(() => apiRequest<{ active: ScrapeActivity[]; recent: ScrapeActivity[]; events: ScrapeLiveEvent[]; generated_at: string }>("/scraping/activity?limit=40"), []);
  const activityResource = useApi(activityLoader, [activityLoader]);
  const jobs = listFrom<Job>(resource.data, ["jobs"]);
  const activeCrawls = activityResource.data?.active ?? [];
  const recentActivity = activityResource.data?.recent ?? [];
  const serverEvents = activityResource.data?.events ?? [];
  const [streamEvents, setStreamEvents] = useState<ScrapeLiveEvent[]>([]);
  const [clockNow, setClockNow] = useState(() => Date.now());
  const total = totalFrom(resource.data, jobs);
  const summary = resource.data && typeof resource.data === "object" && "summary" in resource.data
    ? (resource.data as { summary?: Record<string, unknown> }).summary
    : undefined;

  useEffect(() => {
    const interval = window.setInterval(() => {
      void resource.reload();
      void activityResource.reload();
    }, 2_000);
    return () => window.clearInterval(interval);
  }, [activityResource.reload, resource.reload]);

  useEffect(() => {
    const interval = window.setInterval(() => setClockNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    const receiveScrapeEvent = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (!detail || typeof detail !== "object") return;
      const audit = detail as Record<string, unknown>;
      const after = audit.after && typeof audit.after === "object" ? audit.after as Record<string, unknown> : audit;
      const eventType = String(after.event_type ?? audit.action ?? "");
      if (!eventType) return;
      const item: ScrapeLiveEvent = {
        ...(after as unknown as ScrapeLiveEvent),
        id: String(audit.id ?? after.id ?? `${after.attempt_id ?? "scrape"}:${after.occurred_at ?? Date.now()}:${eventType}`),
        event_type: eventType,
        occurred_at: String(after.occurred_at ?? audit.created_at ?? new Date().toISOString()),
      };
      setStreamEvents((current) => [item, ...current.filter((row) => row.id !== item.id)].slice(0, 40));
    };
    window.addEventListener("linkos:scrape", receiveScrapeEvent);
    return () => window.removeEventListener("linkos:scrape", receiveScrapeEvent);
  }, []);

  async function retry(job: Job) {
    setRetrying(job.id);
    setActionError(null);
    try {
      await apiRequest(`/jobs/${job.id}/retry`, { method: "POST" });
      await resource.reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "The job could not be retried.");
    } finally {
      setRetrying(null);
    }
  }

  const countFromSummary = (key: string, fallback: number) => {
    const value = summary?.[key];
    return typeof value === "number" ? value : fallback;
  };
  const pending = countFromSummary("queued", jobs.filter((job) => ["queued", "pending", "retrying"].includes(job.status)).length);
  const active = countFromSummary("active", jobs.filter((job) => ["running", "leased", "scraping"].includes(job.status)).length);
  const failed = countFromSummary("failed", jobs.filter((job) => ["failed", "error", "dead"].includes(job.status)).length);
  const firstVisible = total > 0 ? page * pageSize + 1 : 0;
  const lastVisible = Math.min(page * pageSize + jobs.length, total);
  const hasPrevious = page > 0;
  const hasNext = lastVisible < total;
  const generatedAt = activityResource.data?.generated_at ? Date.parse(activityResource.data.generated_at) : clockNow;
  const liveElapsed = (item: ScrapeActivity) => item.elapsed_seconds + Math.max(0, Math.floor((clockNow - generatedAt) / 1_000));
  const elapsedLabel = (seconds: number) => {
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
  };
  const liveEvents = [...streamEvents, ...serverEvents]
    .filter((item, index, rows) => rows.findIndex((candidate) => candidate.id === item.id) === index)
    .sort((left, right) => Date.parse(right.occurred_at ?? "") - Date.parse(left.occurred_at ?? ""))
    .slice(0, 40);
  const eventDescription = (item: ScrapeLiveEvent) => {
    if (item.event_type === "scrape_started") return "Crawler leased the domain";
    if (item.event_type === "page_started") return `Opening page ${formatNumber((item.pages_attempted ?? 0) + 1)} of ${formatNumber(item.max_pages ?? 20)}`;
    if (item.event_type === "email_found") return `${formatNumber(item.emails_found ?? 0)} public email${item.emails_found === 1 ? "" : "s"} found so far`;
    if (item.event_type === "scrape_completed") return `${titleCase(item.status ?? "complete")} · ${formatNumber(item.pages_crawled ?? 0)} pages · ${formatNumber(item.emails_found ?? 0)} emails`;
    if (item.event_type === "scrape_failed") return item.error_code ? titleCase(item.error_code) : "Crawl failed";
    const response = item.status_code ? ` · HTTP ${item.status_code}` : "";
    return `${titleCase(item.page_status ?? "page crawled")} · ${formatNumber(item.pages_crawled ?? 0)} successful${response}`;
  };

  return (
    <div className="space-y-8 animate-enter">
      <PageHeader
        eyebrow="Bounded workers"
        title="Scraping"
        description="Inspect the durable crawl backlog, active leases and retry history. Host pacing and robots rules are enforced by workers."
        actions={
          <select className="field min-w-48" aria-label="Filter scraping jobs" value={status} onChange={(event) => { setStatus(event.target.value); setPage(0); }}>
            <option value="">All job states</option><option value="queued">Queued</option><option value="active">Active</option><option value="failed">Failed</option><option value="completed">Completed</option>
          </select>
        }
      />

      <section className="grid overflow-hidden rounded-xl border border-slate-200 bg-white lg:grid-cols-[minmax(0,1.35fr)_minmax(320px,.65fr)]" aria-label="Live scraping activity">
        <div className="min-w-0 border-b border-slate-200 lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
            <div>
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-emerald-700">
                <span className="relative flex size-2"><span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-50" /><span className="relative inline-flex size-2 rounded-full bg-emerald-600" /></span>
                Live crawl
              </div>
              <h2 className="mt-1 text-lg font-semibold tracking-tight text-zinc-950">Sites being scraped now</h2>
            </div>
            <span className="font-mono text-sm text-slate-500">{formatNumber(activeCrawls.length)} active</span>
          </div>

          {activityResource.loading && !activityResource.data ? (
            <div className="space-y-px bg-slate-100" aria-label="Loading active crawls">
              {[0, 1].map((item) => <div className="h-24 animate-pulse bg-slate-50" key={item} />)}
            </div>
          ) : activityResource.error && !activityResource.data ? (
            <div className="p-5"><ErrorState error={activityResource.error} onRetry={activityResource.reload} title="Live crawl activity is unavailable" /></div>
          ) : activeCrawls.length ? (
            <div className="divide-y divide-slate-100">
              {activeCrawls.map((item) => (
                <div className="relative overflow-hidden px-5 py-4" key={item.id}>
                  <div className="absolute inset-x-0 bottom-0 h-px overflow-hidden bg-emerald-100"><span className="block h-full w-1/3 animate-[shimmer_1.8s_ease-in-out_infinite] bg-emerald-500" /></div>
                  <div className="flex min-w-0 items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <GlobeSimple size={18} weight="regular" className="shrink-0 text-emerald-700" />
                        <p className="truncate font-semibold text-zinc-950">{item.domain}</p>
                      </div>
                      <p className="mt-2 font-mono text-[11px] uppercase tracking-[0.08em] text-slate-500">
                        Attempt {formatNumber(item.job_attempts ?? item.attempt_number)} of {formatNumber(item.job_max_attempts ?? 3)} · lease protected
                      </p>
                      <p className="mt-2 max-w-2xl truncate font-mono text-[11px] text-slate-500" title={item.current_url ?? undefined}>
                        {item.current_url || "Preparing first public page…"}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">
                        {formatNumber(item.pages_crawled)} of {formatNumber(item.max_pages ?? 20)} pages · {formatNumber(item.emails_found)} emails · {titleCase(item.page_status ?? item.last_event ?? "starting")}
                      </p>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className="font-mono text-lg font-semibold tabular-nums text-zinc-950">{elapsedLabel(liveElapsed(item))}</p>
                      <p className="mt-1 text-xs text-slate-500">Started {formatDate(item.started_at, true)}</p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex min-h-36 items-center gap-4 px-5 py-6">
              <CheckCircle size={28} weight="regular" className="text-emerald-700" />
              <div><p className="font-medium text-zinc-950">No active crawl leases</p><p className="mt-1 text-sm text-slate-500">Workers will appear here as soon as a domain is leased.</p></div>
            </div>
          )}
        </div>

        <div className="min-w-0">
          <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
            <div className="flex items-center gap-2"><Pulse size={18} weight="regular" className="text-slate-500" /><h2 className="font-semibold tracking-tight text-zinc-950">Live event stream</h2></div>
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-slate-400">Instant · 2s fallback</span>
          </div>
          <div className="max-h-[27rem] divide-y divide-slate-100 overflow-y-auto" aria-live="polite">
            {liveEvents.length ? liveEvents.map((item) => (
              <div className="px-5 py-3.5" key={item.id}>
                <div className="flex items-start gap-3">
                  {item.event_type === "email_found" ? <EnvelopeSimple size={17} weight="fill" className="mt-0.5 shrink-0 text-emerald-600" /> : item.event_type === "scrape_failed" ? <WarningCircle size={17} weight="fill" className="mt-0.5 shrink-0 text-rose-600" /> : item.event_type === "scrape_completed" ? <CheckCircle size={17} weight="fill" className="mt-0.5 shrink-0 text-emerald-600" /> : <Pulse size={17} weight="regular" className="mt-0.5 shrink-0 text-sky-600" />}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-3"><p className="truncate text-sm font-medium text-zinc-950">{item.domain ?? "Crawler"}</p><span className="shrink-0 font-mono text-[10px] text-slate-400">{formatDate(item.occurred_at, true)}</span></div>
                    <p className="mt-1 text-xs text-slate-600">{eventDescription(item)}</p>
                    {item.current_url && <p className="mt-1 truncate font-mono text-[10px] text-slate-400" title={item.current_url}>{item.current_url}</p>}
                  </div>
                </div>
              </div>
            )) : recentActivity.length ? recentActivity.map((item) => (
              <div className="px-5 py-3.5" key={item.id}>
                <div className="flex items-start gap-3">
                  {item.status === "succeeded" ? <CheckCircle size={17} weight="fill" className="mt-0.5 shrink-0 text-emerald-600" /> : item.status === "no_email" ? <EnvelopeSimple size={17} weight="regular" className="mt-0.5 shrink-0 text-slate-400" /> : <WarningCircle size={17} weight="fill" className="mt-0.5 shrink-0 text-rose-600" />}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-3"><p className="truncate text-sm font-medium text-zinc-950">{item.domain}</p><span className="shrink-0 font-mono text-[10px] text-slate-400">{formatDate(item.completed_at, true)}</span></div>
                    <p className="mt-1 text-xs text-slate-500">{titleCase(item.status)} · {formatNumber(item.pages_crawled)} pages · {formatNumber(item.emails_found)} emails · {elapsedLabel(item.elapsed_seconds)}</p>
                    {(item.error_code || item.error_detail) && <p className="mt-1 line-clamp-1 text-xs text-rose-700">{item.error_code ?? item.error_detail}</p>}
                  </div>
                </div>
              </div>
            )) : <p className="px-5 py-6 text-sm text-slate-500">Page visits and crawler outcomes will appear here as soon as a domain is leased.</p>}
          </div>
        </div>
      </section>

      <section className="grid gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Jobs in filter" value={formatNumber(total)} detail="Full matching queue" />
        <Metric label="Backlog" value={formatNumber(pending)} detail="All jobs waiting for a lease" />
        <Metric label="Active" value={formatNumber(active)} detail="One request per host" tone="good" />
        <Metric label="Failed" value={formatNumber(failed)} detail="Retry or inspect" tone={failed ? "bad" : "neutral"} />
      </section>

      <div className="grid gap-4 border-y border-slate-200 py-5 md:grid-cols-4">
        {["8 domain workers", "1 second host delay", "20 pages maximum", "3 attempts, 10s timeout"].map((label, index) => (
          <div className="flex items-center gap-3 text-sm text-slate-600" key={label}>
            {index === 0 ? <HardDrives size={18} weight="regular" className="text-emerald-700" /> : <Clock size={18} weight="regular" className="text-slate-400" />}
            {label}
          </div>
        ))}
      </div>

      <p className="max-w-[80ch] text-sm leading-6 text-slate-600">
        LINK OS has no business-level intake cap. Every eligible imported domain is stored and processed; this table uses pages of {formatNumber(pageSize)} rows to keep the console responsive.
      </p>

      {actionError && <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">{actionError}</div>}

      {resource.loading && !resource.data ? (
        <PageSkeleton />
      ) : resource.error && !resource.data ? (
        <ErrorState error={resource.error} onRetry={resource.reload} title="Scrape jobs are unavailable" />
      ) : jobs.length ? (
        <div className="space-y-4">
          <TableShell label="Scraping jobs">
          <thead><tr><th className={tableHeaderClass}>Domain / job</th><th className={tableHeaderClass}>State</th><th className={tableHeaderClass}>Attempts</th><th className={tableHeaderClass}>Lease / next run</th><th className={tableHeaderClass}>Last error</th><th className={tableHeaderClass}>Action</th></tr></thead>
          <tbody>
            {jobs.map((job) => (
              <tr className="bg-white" key={job.id}>
                <td className={tableCellClass}><p className="font-medium text-zinc-950">{job.domain ?? `Job ${job.id}`}</p><p className="mt-1 font-mono text-[11px] text-slate-400">{titleCase(job.job_type ?? "scrape")}</p></td>
                <td className={tableCellClass}><StatusBadge status={job.status} /></td>
                <td className={`${tableCellClass} table-number`}>{formatNumber(job.attempts ?? 0)} / {formatNumber(job.max_attempts ?? 3)}</td>
                <td className={tableCellClass}>{formatDate(job.leased_until ?? job.run_after, true)}</td>
                <td className={`${tableCellClass} max-w-sm`}><p className="line-clamp-2 text-xs leading-5 text-slate-600">{job.last_error ?? "—"}</p></td>
                <td className={tableCellClass}>
                  {(["failed", "error", "dead"].includes(job.status) || (job.attempts ?? 0) > 0) && (
                    <Button type="button" disabled={retrying === job.id} onClick={() => void retry(job)} icon={<ArrowClockwise size={15} weight="regular" />}>
                      {retrying === job.id ? "Retrying…" : "Retry"}
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
          </TableShell>
          <div className="flex flex-col gap-3 border-t border-slate-200 pt-4 sm:flex-row sm:items-center sm:justify-between">
            <p className="font-mono text-xs text-slate-500">
              Showing {formatNumber(firstVisible)}–{formatNumber(lastVisible)} of {formatNumber(total)} matching jobs
            </p>
            <div className="flex gap-2">
              <Button type="button" disabled={!hasPrevious} onClick={() => setPage((current) => Math.max(0, current - 1))} icon={<CaretLeft size={15} weight="regular" />}>Previous</Button>
              <Button type="button" disabled={!hasNext} onClick={() => setPage((current) => current + 1)} icon={<CaretRight size={15} weight="regular" />}>Next</Button>
            </div>
          </div>
        </div>
      ) : (
        <EmptyState title={status ? "No jobs in this state" : "The scrape queue is empty"} description={status ? "Choose another state to inspect the queue." : "Accepted domains will create bounded jobs here."} />
      )}
    </div>
  );
}
