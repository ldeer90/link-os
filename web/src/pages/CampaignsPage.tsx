import { useCallback, useState } from "react";
import { CheckCircle, LockKey, Pause, Play, ShieldWarning, UploadSimple } from "@phosphor-icons/react";
import { useSystem } from "../context/SystemContext";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom } from "../lib/api";
import { formatNumber, formatPercent } from "../lib/format";
import { Campaign, SenderHealth } from "../types";
import {
  Button,
  EmptyState,
  ErrorState,
  InlineNotice,
  Metric,
  PageHeader,
  PageSkeleton,
  StatusBadge,
  TableShell,
  tableCellClass,
  tableHeaderClass,
} from "../components/ui";

export function CampaignsPage() {
  const { health, actionPending, pause, resume } = useSystem();
  const [confirmResume, setConfirmResume] = useState(false);
  const [confirmPrepare, setConfirmPrepare] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [prepareResult, setPrepareResult] = useState<string | null>(null);
  const [estimate, setEstimate] = useState<{ allowed: boolean; available: Record<string, number>; healthy_unassigned_senders: number } | null>(null);
  const [launchId, setLaunchId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const loader = useCallback(() => apiRequest<unknown>("/campaigns"), []);
  const resource = useApi(loader, [loader]);
  const campaigns = listFrom<Campaign>(resource.data, ["campaigns", "batches"]);
  const senders = listFrom<SenderHealth>(health?.senders, ["senders"]);
  const paused = health?.outreach_paused ?? true;
  const blockers = health?.blocking_reasons ?? [];
  const healthySenders = senders.filter((sender) => sender.healthy || sender.status === "healthy");

  async function handlePause() {
    setActionError(null);
    try {
      await pause("Emergency pause from Campaigns console");
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Outreach could not be paused.");
    }
  }

  async function handleResume() {
    setActionError(null);
    try {
      await resume();
      setConfirmResume(false);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Outreach remains paused.");
    }
  }

  async function handleEstimateSet() {
    setActionError(null);
    try {
      setEstimate(await apiRequest("/campaign-sets/estimate", { method: "POST", body: "{}" }));
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Campaign availability could not be estimated.");
    }
  }

  async function handlePreparePilot() {
    setActionError(null);
    setPrepareResult(null);
    setPreparing(true);
    try {
      const result = await apiRequest<{ id: string; job_ids: string[] }>("/campaign-sets", {
        method: "POST",
        body: JSON.stringify({
          batch_size: 25,
          confirm_verification_skipped: true,
        }),
      });
      setLaunchId(result.id);
      setPrepareResult(`Three paused campaigns queued as ${result.job_ids.length} durable jobs.`);
      setConfirmPrepare(false);
      window.setTimeout(() => void resource.reload(), 1500);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "The campaign set could not be prepared.");
    } finally {
      setPreparing(false);
    }
  }

  async function handleActivateSet() {
    const id = launchId ?? campaigns.find((campaign) => campaign.launch_id)?.launch_id;
    if (!id) return;
    setActionError(null);
    try {
      await apiRequest(`/campaign-sets/${id}/activate`, { method: "POST", body: JSON.stringify({ confirm_activation: true }) });
      setPrepareResult("Activation queued for all three readback-complete campaigns.");
      window.setTimeout(() => void resource.reload(), 1500);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "The campaign set could not be activated.");
    }
  }

  async function handlePauseCampaign(id: string | number) {
    setActionError(null);
    try {
      await apiRequest(`/campaigns/${id}/pause`, { method: "POST", body: JSON.stringify({ reason: "Paused from Campaigns console" }) });
      await resource.reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "The campaign could not be paused.");
    }
  }

  return (
    <div className="space-y-8 animate-enter">
      <PageHeader
        eyebrow="Fail-closed delivery"
        title="Campaigns"
        description="Reserve deterministic 250-contact batches, inspect sender capacity and stop outreach globally without touching scraping or reply sync."
        actions={paused ? (
          <Button variant="primary" type="button" onClick={() => setConfirmResume(true)} disabled={actionPending} icon={<Play size={16} weight="fill" />}>Review resume</Button>
        ) : (
          <Button variant="danger" type="button" onClick={() => void handlePause()} disabled={actionPending} icon={<Pause size={16} weight="fill" />}>Emergency pause</Button>
        )}
      />

      {actionError && <InlineNotice tone="danger" title="Outreach state unchanged">{actionError}</InlineNotice>}
      {prepareResult && <InlineNotice tone="success" title="Campaign-set update">{prepareResult}</InlineNotice>}
      {paused && (
        <InlineNotice tone="warning" title="Automatic activation is blocked">
          {health?.pause_reason ?? "Outreach is globally paused."} {blockers.length ? blockers.join(" ") : "A healthy Instantly sender and passing safety checks are required to resume."}
        </InlineNotice>
      )}
      {!paused && blockers.length > 0 && <InlineNotice tone="danger" title="A safety threshold has tripped">{blockers.join(" ")}</InlineNotice>}

      <section className="grid gap-5 rounded-xl border border-slate-200 bg-white p-5 shadow-panel lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-emerald-700">Segmented public evidence</p>
          <h2 className="mt-2 font-semibold tracking-tight text-zinc-950">One AU and two travel/global campaigns</h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-600">Reserve 75 distinct publishers across three healthy inboxes. Addresses require ≥0.90 public evidence and an editorial-role match; deliverability verification remains explicitly skipped.</p>
          {estimate && <div className="mt-4 grid gap-px overflow-hidden rounded-lg border border-slate-200 bg-slate-200 sm:grid-cols-3"><div className="bg-slate-50 p-3"><p className="text-xs text-slate-500">AU available</p><p className="mt-1 font-mono text-lg text-zinc-950">{formatNumber(estimate.available.au_publishers)} / 25</p></div><div className="bg-slate-50 p-3"><p className="text-xs text-slate-500">Travel/global available</p><p className="mt-1 font-mono text-lg text-zinc-950">{formatNumber(estimate.available.travel_global)} / 50</p></div><div className="bg-slate-50 p-3"><p className="text-xs text-slate-500">Unused healthy inboxes</p><p className="mt-1 font-mono text-lg text-zinc-950">{formatNumber(estimate.healthy_unassigned_senders)} / 3</p></div></div>}
        </div>
        <div className="flex flex-wrap gap-2 lg:justify-end"><Button type="button" onClick={() => void handleEstimateSet()}>Check availability</Button><Button type="button" variant="primary" onClick={() => setConfirmPrepare(true)} disabled={preparing || healthySenders.length < 3 || estimate?.allowed === false} icon={<UploadSimple size={16} weight="bold" />}>Prepare three paused campaigns</Button>{(launchId || campaigns.some((campaign) => campaign.launch_id)) && <Button type="button" variant="primary" onClick={() => void handleActivateSet()} icon={<Play size={16} weight="fill" />}>Activate campaign set</Button>}</div>
      </section>

      <section className="grid gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Managed batches" value={formatNumber(campaigns.filter((campaign) => campaign.managed !== false).length)} detail="LINK OS tagged" />
        <Metric label="Healthy senders" value={`${healthySenders.length}/${senders.length}`} detail="One active campaign each" tone={healthySenders.length ? "good" : "bad"} />
        <Metric label="Reserved members" value={formatNumber(campaigns.reduce((sum, campaign) => sum + (campaign.member_count ?? 0), 0))} detail="Transactionally deduplicated" />
        <Metric label="Replies" value={formatNumber(campaigns.reduce((sum, campaign) => sum + (campaign.reply_count ?? 0), 0))} detail="Polled every ten minutes" />
      </section>

      <section className="grid gap-8 xl:grid-cols-[minmax(0,1.2fr)_minmax(18rem,0.8fr)]">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-950">Campaign batches</h2>
          <p className="mt-1 mb-4 text-sm text-slate-600">Built paused, count-verified, then activated only when every gate passes.</p>
          {resource.loading && !resource.data ? (
            <PageSkeleton />
          ) : resource.error && !resource.data ? (
            <ErrorState error={resource.error} onRetry={resource.reload} title="Campaign batches are unavailable" />
          ) : campaigns.length ? (
            <TableShell label="Campaign batches">
              <thead><tr><th className={tableHeaderClass}>Batch</th><th className={tableHeaderClass}>State</th><th className={tableHeaderClass}>Sender</th><th className={tableHeaderClass}>Members</th><th className={tableHeaderClass}>Reply</th><th className={tableHeaderClass}>Bounce</th></tr></thead>
              <tbody>
                {campaigns.map((campaign) => (
                  <tr className="bg-white" key={campaign.id}>
                    <td className={tableCellClass}><p className="font-medium text-zinc-950">{campaign.name}</p><p className="mt-1 font-mono text-[11px] uppercase tracking-wide text-emerald-700">{campaign.segment?.replace("_", " ") ?? "legacy pilot"}</p>{campaign.blocking_reasons?.length ? <p className="mt-1 max-w-xs text-xs leading-5 text-rose-700">{campaign.blocking_reasons.join(" · ")}</p> : null}</td>
                    <td className={tableCellClass}><StatusBadge status={campaign.status} /></td>
                    <td className={`${tableCellClass} font-mono text-xs`}>{campaign.sender_email ?? "Waiting for capacity"}</td>
                    <td className={`${tableCellClass} table-number`}>{formatNumber(campaign.uploaded_count ?? campaign.member_count)} / {formatNumber(campaign.member_count)}</td>
                    <td className={`${tableCellClass} table-number`}>{formatNumber(campaign.reply_count)}</td>
                    <td className={`${tableCellClass} table-number`}><p className={campaign.ignore_hard_bounces ? "font-semibold text-rose-700" : undefined}>{campaign.ignore_hard_bounces ? "Ignored" : `${formatNumber(campaign.hard_bounce_count)} / ${formatNumber(campaign.hard_bounce_limit ?? 3)}`}</p>{campaign.provider_bounce_protection_disabled && <p className="mt-1 text-[11px] text-rose-700">Provider protection off</p>}{campaign.status === "active" && <Button type="button" onClick={() => void handlePauseCampaign(campaign.id)} variant="danger" className="mt-2">Pause</Button>}</td>
                  </tr>
                ))}
              </tbody>
            </TableShell>
          ) : (
            <EmptyState title="No LINK OS batches" description="Verified contacts will be reserved into deterministic batches when sender capacity becomes available." />
          )}
        </div>

        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-950">Sender health</h2>
          <p className="mt-1 mb-4 text-sm text-slate-600">A connection error pauses every managed campaign.</p>
          {senders.length ? (
            <div className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
              {senders.map((sender) => {
                const healthy = sender.healthy || sender.status === "healthy";
                return (
                  <div className="p-4" key={sender.id ?? sender.email}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0"><p className="truncate font-mono text-xs font-medium text-zinc-950">{sender.email}</p><p className="mt-1 text-xs text-slate-500">{formatNumber(sender.sent_today)} / {formatNumber(sender.daily_limit)} today</p></div>
                      <StatusBadge status={healthy ? "healthy" : sender.status} />
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-3 text-xs"><div><span className="text-slate-500">Bounce </span><span className="font-mono text-zinc-950">{formatPercent(sender.bounce_rate)}</span></div><div><span className="text-slate-500">Unsubscribe </span><span className="font-mono text-zinc-950">{formatPercent(sender.unsubscribe_rate)}</span></div></div>
                    {sender.blocking_reason && <p className="mt-3 text-xs leading-5 text-rose-700">{sender.blocking_reason}</p>}
                  </div>
                );
              })}
            </div>
          ) : (
            <EmptyState title="Sender health unavailable" description="Reconnect at least one Instantly sender before any managed campaign can activate." />
          )}
        </div>
      </section>

      <section className="grid gap-4 border-y border-slate-200 py-6 sm:grid-cols-2 xl:grid-cols-4">
        {["Weekdays, 09:00–17:00 Melbourne", "Six text-only steps over 14 days", "30 total emails per healthy sender", "Pause one campaign after 3 hard bounces"].map((rule, index) => (
          <div className="flex items-start gap-3 text-sm leading-6 text-slate-600" key={rule}>
            {index < 3 ? <CheckCircle size={18} weight="regular" className="mt-0.5 shrink-0 text-emerald-700" /> : <ShieldWarning size={18} weight="regular" className="mt-0.5 shrink-0 text-amber-700" />}
            {rule}
          </div>
        ))}
      </section>

      {confirmResume && (
        <div className="fixed inset-0 grid place-items-center bg-zinc-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-labelledby="resume-title">
          <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-6 shadow-2xl">
            <span className="grid size-10 place-items-center rounded-lg bg-amber-100 text-amber-800"><LockKey size={20} weight="regular" /></span>
            <h2 id="resume-title" className="mt-5 text-xl font-semibold tracking-tight text-zinc-950">Resume managed outreach?</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">The API will reject this request if sender health, verification, upload counts or rolling campaign thresholds are unsafe.</p>
            {blockers.length > 0 && <div className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm leading-6 text-rose-900">{blockers.join(" ")}</div>}
            <div className="mt-6 flex justify-end gap-2">
              <Button type="button" onClick={() => setConfirmResume(false)}>Cancel</Button>
              <Button variant="primary" type="button" disabled={actionPending || blockers.length > 0} onClick={() => void handleResume()} icon={<Play size={16} weight="fill" />}>
                {actionPending ? "Checking gates…" : "Confirm resume"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {confirmPrepare && (
        <div className="fixed inset-0 grid place-items-center bg-zinc-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-labelledby="prepare-title">
          <div className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-6 shadow-2xl">
            <span className="grid size-10 place-items-center rounded-lg bg-amber-100 text-amber-800"><ShieldWarning size={20} weight="fill" /></span>
            <h2 id="prepare-title" className="mt-5 text-xl font-semibold tracking-tight text-zinc-950">Prepare 75 unverified contacts?</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">LINK OS will reserve 25 AU and 50 travel/global publishers across three paused campaigns. Every address has strong public-page evidence, but mailbox deliverability has not been verified.</p>
            <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-950">Preparation creates no sends. Activation remains a separate readback-gated action.</div>
            <div className="mt-6 flex justify-end gap-2">
              <Button type="button" onClick={() => setConfirmPrepare(false)} disabled={preparing}>Cancel</Button>
              <Button variant="primary" type="button" disabled={preparing} onClick={() => void handlePreparePilot()} icon={<UploadSimple size={16} weight="bold" />}>
                {preparing ? "Preparing…" : "Confirm 75-contact preparation"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
