import { ArrowClockwise, CheckCircle, Database, Lock, PlugsConnected, WarningCircle } from "@phosphor-icons/react";
import { useSystem } from "../context/SystemContext";
import { formatDate, titleCase } from "../lib/format";
import { IntegrationState } from "../types";
import { Button, ErrorState, InlineNotice, PageHeader, PageSkeleton, StatusBadge } from "../components/ui";

function integrationsFrom(value: IntegrationState[] | Record<string, IntegrationState | string> | undefined): IntegrationState[] {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  return Object.entries(value).map(([name, item]) => typeof item === "string" ? { name, status: item } : { ...item, name: item.name ?? name });
}

export function SettingsPage() {
  const { health, loading, error, reload } = useSystem();
  if (loading && !health) return <PageSkeleton />;
  const integrations = integrationsFrom(health?.integrations);
  const core = [
    { name: "PostgreSQL", value: health?.database },
    { name: "Background worker", value: health?.worker },
  ].map(({ name, value }): IntegrationState => typeof value === "string" ? { name, status: value } : { name, status: value?.status ?? "unknown", ...value });

  return (
    <div className="space-y-8 animate-enter">
      <PageHeader eyebrow="System configuration" title="Settings & integrations" description="Inspect local service health and external write gates without displaying credential values." actions={<Button type="button" onClick={() => void reload()} icon={<ArrowClockwise size={16} weight="regular" />}>Refresh health</Button>} />
      {error && !health && <ErrorState error={error} onRetry={reload} title="Integration health is unavailable" />}
      {health?.shadow_mode && <InlineNotice tone="warning" title="Shadow mode is active">Workers may reconcile and project status, but managed campaigns will not activate.</InlineNotice>}

      <section>
        <h2 className="text-lg font-semibold tracking-tight text-zinc-950">Core services</h2><p className="mt-1 text-sm text-slate-600">The API and worker share one authoritative PostgreSQL database.</p>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          {core.map((service) => <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-panel" key={service.name}><div className="flex items-start justify-between gap-4"><span className="grid size-10 place-items-center rounded-lg bg-slate-100 text-slate-600"><Database size={20} weight="regular" /></span><StatusBadge status={service.status} /></div><h3 className="mt-5 font-semibold text-zinc-950">{service.name}</h3>{service.message && <p className="mt-1 text-sm leading-6 text-slate-600">{service.message}</p>}</div>)}
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold tracking-tight text-zinc-950">External integrations</h2><p className="mt-1 text-sm text-slate-600">Monday receives deals only. Instantly delivery stays fail-closed whenever account or campaign state is unsafe.</p>
        {integrations.length ? <div className="mt-4 divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">{integrations.map((integration) => {
          const healthy = ["healthy", "connected", "active", "ok"].includes(integration.status.toLowerCase());
          return <div className="grid gap-4 p-5 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center" key={integration.name}><span className={`grid size-10 place-items-center rounded-lg ${healthy ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"}`}>{healthy ? <CheckCircle size={20} weight="regular" /> : <WarningCircle size={20} weight="regular" />}</span><div><h3 className="font-semibold text-zinc-950">{titleCase(integration.name)}</h3><p className="mt-1 text-sm leading-6 text-slate-600">{integration.message ?? (healthy ? "Connection readback passed." : "Integration requires attention before dependent writes can run.")}</p>{integration.checked_at && <p className="mt-1 font-mono text-[11px] text-slate-400">Checked {formatDate(integration.checked_at, true)}</p>}</div><div className="flex items-center gap-2"><StatusBadge status={integration.status} />{integration.writable === false && <span className="inline-flex items-center gap-1 text-xs text-slate-500"><Lock size={13} weight="regular" /> Read only</span>}</div></div>;
        })}</div> : <div className="mt-4 rounded-xl border border-dashed border-slate-300 p-6"><span className="grid size-10 place-items-center rounded-lg bg-slate-100 text-slate-500"><PlugsConnected size={20} weight="regular" /></span><p className="mt-4 font-medium text-zinc-950">No integration readbacks reported</p><p className="mt-1 text-sm leading-6 text-slate-600">The API should report Instantly, Monday and optional BigQuery replica health here.</p></div>}
      </section>

      <section className="border-y border-slate-200 py-6">
        <h2 className="text-lg font-semibold tracking-tight text-zinc-950">Operational defaults</h2>
        <dl className="mt-5 grid gap-x-8 gap-y-5 sm:grid-cols-2 xl:grid-cols-4">
          {[ ["Scrape concurrency", "8 domains"], ["Host pacing", "1 request / second"], ["Refresh window", "90 days"], ["Campaign batch", "250 contacts"], ["Pilot daily leads", "10 per sender"], ["Reply polling", "10 minutes"], ["Analytics polling", "15 minutes"], ["Catalogue", "Private until listed"] ].map(([label, value]) => <div key={label}><dt className="text-xs text-slate-500">{label}</dt><dd className="mt-1 font-mono text-sm font-medium text-zinc-950">{value}</dd></div>)}
        </dl>
      </section>
    </div>
  );
}
