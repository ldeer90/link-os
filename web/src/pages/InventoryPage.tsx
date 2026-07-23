import { useCallback, useState } from "react";
import { Eye, EyeSlash, Storefront } from "@phosphor-icons/react";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom } from "../lib/api";
import { formatAud, formatDate, formatNumber, titleCase } from "../lib/format";
import { Listing } from "../types";
import { Button, EmptyState, ErrorState, InlineNotice, PageHeader, PageSkeleton, SearchField, StatusBadge, TableShell, tableCellClass, tableHeaderClass } from "../components/ui";

export function InventoryPage() {
  const [search, setSearch] = useState("");
  const [visibility, setVisibility] = useState("");
  const [updating, setUpdating] = useState<string | number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const loader = useCallback(() => apiRequest<unknown>("/listings?page_size=100"), []);
  const resource = useApi(loader, [loader]);
  const listings = listFrom<Listing>(resource.data, ["listings", "inventory"]);
  const visible = listings.filter((listing) => {
    const matchesSearch = !search || [listing.domain, listing.publisher_entity, listing.placement_type].some((value) => value?.toLowerCase().includes(search.toLowerCase()));
    const listingVisibility = listing.visibility ?? (listing.status === "listed" ? "listed" : "private");
    return matchesSearch && (!visibility || listingVisibility === visibility);
  });

  async function updateVisibility(listing: Listing, next: "listed" | "private") {
    setUpdating(listing.id);
    setMessage(null);
    try {
      await apiRequest(`/listings/${listing.id}`, { method: "PATCH", body: JSON.stringify({ status: next, visibility: next }) });
      setMessage(next === "listed" ? `${listing.domain} is now visible in the agency catalogue.` : `${listing.domain} is private and hidden from agencies.`);
      await resource.reload();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Visibility could not be updated.");
    } finally {
      setUpdating(null);
    }
  }

  return (
    <div className="space-y-7 animate-enter">
      <PageHeader eyebrow="Private inventory" title="Inventory & catalogue" description="Approved publisher pricing remains internal until a separate listing action makes it visible to the selected agency tier." />
      <InlineNotice tone="info" title="Deals are private by default">Publisher cost, FX detail and internal pricing evidence are never exposed in the agency catalogue.</InlineNotice>
      {message && <InlineNotice tone="success" title="Visibility updated">{message}</InlineNotice>}
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_14rem]">
        <SearchField value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search inventory" aria-label="Search inventory" />
        <select className="field" value={visibility} onChange={(event) => setVisibility(event.target.value)} aria-label="Filter catalogue visibility"><option value="">All visibility</option><option value="private">Private</option><option value="listed">Listed</option></select>
      </div>
      {resource.loading && !resource.data ? <PageSkeleton /> : resource.error && !resource.data ? <ErrorState error={resource.error} onRetry={resource.reload} title="Inventory is unavailable" /> : visible.length ? (
        <TableShell label="Publisher inventory">
          <thead><tr><th className={tableHeaderClass}>Domain</th><th className={tableHeaderClass}>Placement</th><th className={tableHeaderClass}>Private AUD</th><th className={tableHeaderClass}>Visibility</th><th className={tableHeaderClass}>Enquiries</th><th className={tableHeaderClass}>Updated</th><th className={tableHeaderClass}>Action</th></tr></thead>
          <tbody>{visible.map((listing) => {
            const isListed = (listing.visibility ?? listing.status) === "listed";
            return <tr className="bg-white" key={listing.id}>
              <td className={tableCellClass}><p className="font-medium text-zinc-950">{listing.domain}</p><p className="mt-1 text-xs text-slate-500">{listing.publisher_entity ?? "Independent publisher"}</p></td>
              <td className={tableCellClass}>{titleCase(listing.placement_type)}</td>
              <td className={`${tableCellClass} font-mono font-medium text-zinc-950`}>{formatAud(listing.reseller_price_aud)}</td>
              <td className={tableCellClass}><div className="space-y-1"><StatusBadge status={isListed ? "listed" : "paused"} label={isListed ? titleCase(listing.visibility ?? "listed") : "Private"} />{!isListed && <p className="text-[11px] text-slate-500">Not visible to agencies</p>}</div></td>
              <td className={`${tableCellClass} table-number`}>{formatNumber(listing.enquiries)}</td><td className={tableCellClass}>{formatDate(listing.updated_at)}</td>
              <td className={tableCellClass}><Button type="button" disabled={updating === listing.id} onClick={() => void updateVisibility(listing, isListed ? "private" : "listed")} icon={isListed ? <EyeSlash size={15} weight="regular" /> : <Eye size={15} weight="regular" />}>{isListed ? "Make private" : "List"}</Button></td>
            </tr>;
          })}</tbody>
        </TableShell>
      ) : <EmptyState title="No inventory matches this view" description="Approve an offer privately first, then list it here only when it is ready for agencies." action={<Storefront size={20} weight="regular" />} />}
    </div>
  );
}
