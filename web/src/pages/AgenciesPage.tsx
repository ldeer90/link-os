import { useCallback, useState } from "react";
import { Buildings, EnvelopeSimple } from "@phosphor-icons/react";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom } from "../lib/api";
import { formatDate, formatNumber, titleCase } from "../lib/format";
import { Agency } from "../types";
import { EmptyState, ErrorState, PageHeader, PageSkeleton, SearchField, StatusBadge, TableShell, tableCellClass, tableHeaderClass } from "../components/ui";

export function AgenciesPage() {
  const [search, setSearch] = useState("");
  const loader = useCallback(() => apiRequest<unknown>("/agencies?page_size=100"), []);
  const resource = useApi(loader, [loader]);
  const agencies = listFrom<Agency>(resource.data, ["agencies"]);
  const visible = agencies.filter((agency) => !search || [agency.name, agency.email, agency.visibility_tier].some((value) => value?.toLowerCase().includes(search.toLowerCase())));
  return (
    <div className="space-y-7 animate-enter">
      <PageHeader eyebrow="Catalogue access" title="Agencies" description="Manage who can discover listed inventory and track catalogue enquiries without exposing private publisher terms." />
      <SearchField className="max-w-xl" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search agency or contact" aria-label="Search agencies" />
      {resource.loading && !resource.data ? <PageSkeleton /> : resource.error && !resource.data ? <ErrorState error={resource.error} onRetry={resource.reload} title="Agencies are unavailable" /> : visible.length ? (
        <TableShell label="Agencies">
          <thead><tr><th className={tableHeaderClass}>Agency</th><th className={tableHeaderClass}>Contact</th><th className={tableHeaderClass}>Tier</th><th className={tableHeaderClass}>Status</th><th className={tableHeaderClass}>Enquiries</th><th className={tableHeaderClass}>Last active</th></tr></thead>
          <tbody>{visible.map((agency) => <tr className="bg-white" key={agency.id}><td className={tableCellClass}><span className="inline-flex items-center gap-2 font-medium text-zinc-950"><Buildings size={17} weight="regular" className="text-slate-400" />{agency.name}</span></td><td className={tableCellClass}>{agency.email ? <a className="inline-flex items-center gap-2 text-emerald-700 hover:text-emerald-800" href={`mailto:${agency.email}`}><EnvelopeSimple size={15} weight="regular" />{agency.email}</a> : "—"}</td><td className={tableCellClass}>{titleCase(agency.visibility_tier)}</td><td className={tableCellClass}><StatusBadge status={agency.status ?? "active"} /></td><td className={`${tableCellClass} table-number`}>{formatNumber(agency.enquiry_count)}</td><td className={tableCellClass}>{formatDate(agency.last_active_at, true)}</td></tr>)}</tbody>
        </TableShell>
      ) : <EmptyState title="No agencies match this view" description="Agency catalogue accounts and enquiries will appear here; payments and fulfilment remain out of scope." />}
    </div>
  );
}
