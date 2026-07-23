import { useCallback, useMemo, useState } from "react";
import { ArrowRight, Check, Eye, Paperclip, X } from "@phosphor-icons/react";
import { useApi } from "../hooks/useApi";
import { apiRequest, listFrom } from "../lib/api";
import { formatAud, formatDate, formatNumber, titleCase } from "../lib/format";
import { Offer, Reply } from "../types";
import {
  Button,
  EmptyState,
  ErrorState,
  InlineNotice,
  PageHeader,
  PageSkeleton,
  SearchField,
  StatusBadge,
} from "../components/ui";

interface ReplyOfferPayload {
  repliesPayload: unknown;
  offersPayload: unknown;
}

export function RepliesOffersPage() {
  const [tab, setTab] = useState<"replies" | "offers">("replies");
  const [search, setSearch] = useState("");
  const [selectedReply, setSelectedReply] = useState<Reply | null>(null);
  const [selectedOffer, setSelectedOffer] = useState<Offer | null>(null);
  const [updating, setUpdating] = useState<string | number | null>(null);
  const [actionMessage, setActionMessage] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const loader = useCallback(async (): Promise<ReplyOfferPayload> => {
    const [repliesPayload, offersPayload] = await Promise.all([apiRequest<unknown>("/replies?page_size=100"), apiRequest<unknown>("/offers?page_size=100")]);
    return { repliesPayload, offersPayload };
  }, []);
  const resource = useApi(loader, [loader]);
  const replies = listFrom<Reply>(resource.data?.repliesPayload, ["replies"]);
  const offers = listFrom<Offer>(resource.data?.offersPayload, ["offers"]);
  const normalizedSearch = search.toLowerCase();
  const visibleReplies = useMemo(() => replies.filter((reply) => !normalizedSearch || [reply.domain, reply.from_email, reply.subject, reply.snippet].some((value) => value?.toLowerCase().includes(normalizedSearch))), [replies, normalizedSearch]);
  const visibleOffers = useMemo(() => offers.filter((offer) => !normalizedSearch || [offer.domain, offer.publisher_email, offer.original_currency, offer.placement_type].some((value) => value?.toLowerCase().includes(normalizedSearch))), [offers, normalizedSearch]);

  async function updateOffer(offer: Offer, status: "approved" | "review" | "rejected") {
    setUpdating(offer.id);
    setActionMessage(null);
    try {
      await apiRequest(`/offers/${offer.id}`, { method: "PATCH", body: JSON.stringify({ status }) });
      setActionMessage({ tone: "success", text: status === "approved" ? "Offer approved and kept private until separately listed." : `Offer marked ${status}.` });
      setSelectedOffer(null);
      await resource.reload();
    } catch (reason) {
      setActionMessage({ tone: "danger", text: reason instanceof Error ? reason.message : "The offer could not be updated." });
    } finally {
      setUpdating(null);
    }
  }

  if (resource.loading && !resource.data) return <PageSkeleton />;
  if (resource.error && !resource.data) {
    return <div className="space-y-6"><PageHeader eyebrow="Human review" title="Replies & offers" description="Read reply evidence and approve only unambiguous commercial terms." /><ErrorState error={resource.error} onRetry={resource.reload} /></div>;
  }

  const pendingOffers = offers.filter((offer) => ["review", "pending"].includes(offer.status)).length;
  const unresolvedReplies = replies.filter((reply) => !["resolved", "approved", "rejected"].includes(reply.review_status ?? "review")).length;

  return (
    <div className="space-y-7 animate-enter">
      <PageHeader
        eyebrow="Human review"
        title="Replies & offers"
        description="Every inbound reply is retained with thread evidence. LINK OS extracts terms, but never replies to or negotiates with a publisher."
      />

      <div className="grid gap-4 md:grid-cols-[auto_minmax(0,1fr)] md:items-center">
        <div className="inline-flex w-fit rounded-lg border border-slate-200 bg-white p-1 shadow-sm" role="tablist" aria-label="Reply views">
          <button className={`focus-ring rounded-md px-3 py-2 text-sm font-medium ${tab === "replies" ? "bg-zinc-950 text-white" : "text-slate-600 hover:bg-slate-50"}`} type="button" role="tab" aria-selected={tab === "replies"} onClick={() => setTab("replies")}>Replies <span className="ml-1 font-mono text-xs opacity-70">{formatNumber(unresolvedReplies)}</span></button>
          <button className={`focus-ring rounded-md px-3 py-2 text-sm font-medium ${tab === "offers" ? "bg-zinc-950 text-white" : "text-slate-600 hover:bg-slate-50"}`} type="button" role="tab" aria-selected={tab === "offers"} onClick={() => setTab("offers")}>Offers <span className="ml-1 font-mono text-xs opacity-70">{formatNumber(pendingOffers)}</span></button>
        </div>
        <SearchField className="md:justify-self-end md:w-80" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search publisher or domain" aria-label="Search replies and offers" />
      </div>

      {actionMessage && <InlineNotice tone={actionMessage.tone} title={actionMessage.tone === "success" ? "Offer updated" : "Offer unchanged"}>{actionMessage.text}</InlineNotice>}

      {tab === "replies" ? (
        visibleReplies.length ? (
          <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
            {visibleReplies.map((reply) => (
              <button className="focus-ring grid w-full gap-3 border-b border-slate-100 px-5 py-4 text-left transition hover:bg-slate-50 last:border-0 md:grid-cols-[minmax(0,1fr)_auto]" type="button" key={reply.id} onClick={() => setSelectedReply(reply)}>
                <div className="min-w-0">
                  <div className="flex items-center gap-2"><p className="truncate text-sm font-semibold text-zinc-950">{reply.domain ?? reply.from_email ?? "Unknown publisher"}</p>{reply.has_attachments && <Paperclip size={15} weight="regular" className="shrink-0 text-slate-400" />}</div>
                  <p className="mt-1 truncate text-sm text-slate-700">{reply.subject ?? "No subject"}</p>
                  <p className="mt-1 line-clamp-1 text-xs leading-5 text-slate-500">{reply.snippet ?? "Reply evidence available in the full thread."}</p>
                </div>
                <div className="flex items-center gap-3 md:flex-col md:items-end md:gap-1"><StatusBadge status={reply.review_status ?? "review"} /><span className="font-mono text-[11px] text-slate-500">{formatDate(reply.received_at, true)}</span></div>
              </button>
            ))}
          </div>
        ) : <EmptyState title="No replies match this view" description="New inbound replies are synchronised idempotently every ten minutes." />
      ) : (
        visibleOffers.length ? (
          <div className="grid gap-4 lg:grid-cols-2">
            {visibleOffers.map((offer) => (
              <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-panel" key={offer.id}>
                <div className="flex items-start justify-between gap-4"><div><p className="text-base font-semibold text-zinc-950">{offer.domain}</p><p className="mt-1 font-mono text-xs text-slate-500">{offer.publisher_email ?? "Email retained on publisher entity"}</p></div><StatusBadge status={offer.status} /></div>
                <dl className="mt-6 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-slate-200 bg-slate-200">
                  <div className="bg-slate-50 p-3"><dt className="text-[11px] uppercase tracking-[0.1em] text-slate-500">Publisher asks</dt><dd className="mt-1 font-mono text-lg font-medium text-zinc-950">{offer.original_amount == null ? "—" : `${formatNumber(offer.original_amount)} ${offer.original_currency ?? ""}`}</dd></div>
                  <div className="bg-slate-50 p-3"><dt className="text-[11px] uppercase tracking-[0.1em] text-slate-500">Private AUD</dt><dd className="mt-1 font-mono text-lg font-medium text-emerald-700">{formatAud(offer.reseller_price_aud)}</dd></div>
                </dl>
                <div className="mt-4 flex items-center justify-between gap-3"><p className="text-xs text-slate-500">{titleCase(offer.placement_type)} · Confidence {offer.confidence == null ? "—" : `${Math.round(offer.confidence * (offer.confidence <= 1 ? 100 : 1))}%`}</p><Button type="button" onClick={() => setSelectedOffer(offer)} icon={<Eye size={15} weight="regular" />}>Review</Button></div>
              </article>
            ))}
          </div>
        ) : <EmptyState title="No offers match this view" description="Unambiguous prices may be approved automatically, but remain private until separately listed." />
      )}

      {selectedReply && (
        <div className="fixed inset-0 grid place-items-center bg-zinc-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="Reply evidence">
          <div className="w-full max-w-2xl rounded-xl border border-slate-200 bg-white p-6 shadow-2xl">
            <div className="flex items-start justify-between gap-4 border-b border-slate-200 pb-5"><div><p className="font-mono text-[11px] uppercase tracking-[0.13em] text-emerald-700">Inbound reply</p><h2 className="mt-2 text-xl font-semibold text-zinc-950">{selectedReply.subject ?? "No subject"}</h2><p className="mt-1 text-sm text-slate-500">{selectedReply.from_email} · {formatDate(selectedReply.received_at, true)}</p></div><Button variant="ghost" type="button" onClick={() => setSelectedReply(null)} aria-label="Close reply" icon={<X size={18} weight="regular" />} /></div>
            <div className="my-6 max-h-[45dvh] overflow-y-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-5 text-sm leading-7 text-slate-700">{selectedReply.snippet ?? "The full thread body is retained by the API but was not included in this list response."}</div>
            <div className="flex items-center justify-between gap-4"><StatusBadge status={selectedReply.review_status ?? "review"} /><button className="focus-ring inline-flex items-center gap-1.5 rounded text-sm font-medium text-emerald-700" type="button" onClick={() => { setSelectedReply(null); setTab("offers"); }}>Inspect extracted offers <ArrowRight size={15} weight="regular" /></button></div>
          </div>
        </div>
      )}

      {selectedOffer && (
        <div className="fixed inset-0 grid place-items-center bg-zinc-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-labelledby="offer-title">
          <div className="w-full max-w-xl rounded-xl border border-slate-200 bg-white p-6 shadow-2xl">
            <div className="flex items-start justify-between gap-4"><div><p className="font-mono text-[11px] uppercase tracking-[0.13em] text-emerald-700">Offer evidence</p><h2 id="offer-title" className="mt-2 text-xl font-semibold text-zinc-950">{selectedOffer.domain}</h2></div><Button variant="ghost" type="button" onClick={() => setSelectedOffer(null)} aria-label="Close offer review" icon={<X size={18} weight="regular" />} /></div>
            <dl className="mt-6 grid grid-cols-2 gap-5 text-sm"><div><dt className="text-xs text-slate-500">Original terms</dt><dd className="mt-1 font-mono text-zinc-950">{selectedOffer.original_amount ?? "—"} {selectedOffer.original_currency}</dd></div><div><dt className="text-xs text-slate-500">Private reseller price</dt><dd className="mt-1 font-mono font-medium text-emerald-700">{formatAud(selectedOffer.reseller_price_aud)}</dd></div><div><dt className="text-xs text-slate-500">Placement</dt><dd className="mt-1 text-zinc-950">{titleCase(selectedOffer.placement_type)}</dd></div><div><dt className="text-xs text-slate-500">Pricing rule</dt><dd className="mt-1 font-mono text-xs text-zinc-950">{selectedOffer.pricing_rule_version ?? "Current default"}</dd></div></dl>
            <div className="mt-6 rounded-lg border border-slate-200 bg-slate-50 p-4"><p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-500">Reply evidence</p><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-700">{selectedOffer.evidence ?? "No excerpt supplied in this response. Keep in review until evidence is available."}</p></div>
            <InlineNotice tone="info" title="Private by default"><span>Approval does not publish this offer to agencies. Catalogue listing is a separate action.</span></InlineNotice>
            <div className="mt-6 flex flex-wrap justify-end gap-2"><Button type="button" disabled={updating === selectedOffer.id} onClick={() => void updateOffer(selectedOffer, "rejected")} icon={<X size={15} weight="regular" />}>Reject</Button><Button type="button" disabled={updating === selectedOffer.id} onClick={() => void updateOffer(selectedOffer, "review")}>Keep in review</Button><Button variant="primary" type="button" disabled={updating === selectedOffer.id || selectedOffer.original_amount == null || !selectedOffer.original_currency} onClick={() => void updateOffer(selectedOffer, "approved")} icon={<Check size={15} weight="bold" />}>Approve privately</Button></div>
          </div>
        </div>
      )}
    </div>
  );
}
