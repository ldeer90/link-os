import {
  ArrowClockwise,
  CheckCircle,
  Info,
  MagnifyingGlass,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";
import { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { titleCase } from "../lib/format";

const statusClasses: Record<string, string> = {
  active: "border-emerald-200 bg-emerald-50 text-emerald-800",
  healthy: "border-emerald-200 bg-emerald-50 text-emerald-800",
  live: "border-emerald-200 bg-emerald-50 text-emerald-800",
  verified: "border-emerald-200 bg-emerald-50 text-emerald-800",
  offer_approved: "border-emerald-200 bg-emerald-50 text-emerald-800",
  listed: "border-emerald-200 bg-emerald-50 text-emerald-800",
  complete: "border-emerald-200 bg-emerald-50 text-emerald-800",
  approved: "border-emerald-200 bg-emerald-50 text-emerald-800",
  imported: "border-emerald-200 bg-emerald-50 text-emerald-800",
  completed: "border-emerald-200 bg-emerald-50 text-emerald-800",
  replied: "border-sky-200 bg-sky-50 text-sky-800",
  email_found: "border-sky-200 bg-sky-50 text-sky-800",
  uploaded: "border-sky-200 bg-sky-50 text-sky-800",
  contacted: "border-sky-200 bg-sky-50 text-sky-800",
  sent_confirmed: "border-emerald-200 bg-emerald-50 text-emerald-800",
  uploaded_no_send_record: "border-amber-200 bg-amber-50 text-amber-900",
  no_outreach_evidence: "border-slate-200 bg-slate-100 text-slate-700",
  offer_or_deal: "border-emerald-200 bg-emerald-50 text-emerald-800",
  engagement_confirmed: "border-emerald-200 bg-emerald-50 text-emerald-800",
  outreach_ready: "border-sky-200 bg-sky-50 text-sky-800",
  running: "border-sky-200 bg-sky-50 text-sky-800",
  fetching: "border-sky-200 bg-sky-50 text-sky-800",
  filtering: "border-sky-200 bg-sky-50 text-sky-800",
  queueing_scrape: "border-sky-200 bg-sky-50 text-sky-800",
  review: "border-amber-200 bg-amber-50 text-amber-900",
  queued: "border-amber-200 bg-amber-50 text-amber-900",
  campaign_queued: "border-amber-200 bg-amber-50 text-amber-900",
  scraping: "border-amber-200 bg-amber-50 text-amber-900",
  paused: "border-amber-200 bg-amber-50 text-amber-900",
  awaiting_codex_review: "border-amber-200 bg-amber-50 text-amber-900",
  manual_review: "border-amber-200 bg-amber-50 text-amber-900",
  audit_only: "border-slate-200 bg-slate-100 text-slate-700",
  import_queued: "border-sky-200 bg-sky-50 text-sky-800",
  suppressed: "border-rose-200 bg-rose-50 text-rose-800",
  failed: "border-rose-200 bg-rose-50 text-rose-800",
  error: "border-rose-200 bg-rose-50 text-rose-800",
  rejected: "border-rose-200 bg-rose-50 text-rose-800",
  hard_rejected: "border-rose-200 bg-rose-50 text-rose-800",
  credit_state_unknown: "border-rose-200 bg-rose-50 text-rose-800",
  disconnected: "border-rose-200 bg-rose-50 text-rose-800",
  connection_error: "border-rose-200 bg-rose-50 text-rose-800",
  lost: "border-slate-200 bg-slate-100 text-slate-700",
  no_email: "border-slate-200 bg-slate-100 text-slate-700",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const normalized = status.toLowerCase().replace(/[\s-]+/g, "_");
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${
        statusClasses[normalized] ?? "border-slate-200 bg-white text-slate-700"
      }`}
    >
      {label ?? titleCase(status)}
    </span>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
  icon?: ReactNode;
};

export function Button({ className = "", variant = "secondary", icon, children, ...props }: ButtonProps) {
  const variants = {
    primary: "border-emerald-700 bg-emerald-700 text-white hover:bg-emerald-800",
    secondary: "border-slate-300 bg-white text-zinc-900 hover:border-slate-400 hover:bg-slate-50",
    danger: "border-rose-300 bg-rose-50 text-rose-800 hover:bg-rose-100",
    ghost: "border-transparent bg-transparent text-slate-600 hover:bg-slate-100 hover:text-zinc-900",
  };
  return (
    <button
      className={`focus-ring inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium shadow-sm transition duration-200 active:-translate-y-px disabled:cursor-not-allowed disabled:opacity-50 ${variants[variant]} ${className}`}
      {...props}
    >
      {icon}
      {children}
    </button>
  );
}

export function SearchField({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={`relative ${className}`}>
      <MagnifyingGlass size={17} weight="regular" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
      <input className="field pl-9" type="search" {...props} />
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="grid gap-5 border-b border-slate-200 pb-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
      <div>
        {eyebrow && <p className="mb-2 font-mono text-[11px] font-medium uppercase tracking-[0.17em] text-emerald-700">{eyebrow}</p>}
        <h1 className="text-3xl font-semibold tracking-[-0.035em] text-zinc-950 md:text-4xl">{title}</h1>
        <p className="mt-2 max-w-[65ch] text-sm leading-6 text-slate-600 md:text-base">{description}</p>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2 md:justify-end">{actions}</div>}
    </header>
  );
}

export function SectionHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h2 className="text-lg font-semibold tracking-tight text-zinc-950">{title}</h2>
        {description && <p className="mt-1 text-sm text-slate-600">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function Skeleton({ className = "h-5 w-full" }: { className?: string }) {
  return (
    <div className={`relative overflow-hidden rounded-md bg-slate-200/80 ${className}`} aria-hidden="true">
      <div className="absolute inset-y-0 w-1/2 animate-shimmer bg-gradient-to-r from-transparent via-white/70 to-transparent" />
    </div>
  );
}

export function PageSkeleton() {
  return (
    <div aria-label="Loading page" className="space-y-7">
      <div className="space-y-3 border-b border-slate-200 pb-6">
        <Skeleton className="h-3 w-28" />
        <Skeleton className="h-10 w-72 max-w-full" />
        <Skeleton className="h-4 w-[34rem] max-w-full" />
      </div>
      <div className="grid gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((item) => (
          <div className="space-y-4 bg-white p-5" key={item}>
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-8 w-28" />
          </div>
        ))}
      </div>
      <Skeleton className="h-72 w-full rounded-xl" />
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex min-h-52 flex-col items-start justify-center rounded-xl border border-dashed border-slate-300 bg-white/60 p-7">
      <span className="mb-5 inline-flex rounded-lg border border-slate-200 bg-slate-50 p-2 text-slate-500">
        <Info size={20} weight="regular" />
      </span>
      <h3 className="font-semibold text-zinc-950">{title}</h3>
      <p className="mt-1 max-w-[52ch] text-sm leading-6 text-slate-600">{description}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "This view is unavailable" }: { error: Error; onRetry: () => void; title?: string }) {
  return (
    <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-5 text-rose-950">
      <div className="flex items-start gap-3">
        <WarningCircle className="mt-0.5 shrink-0" size={20} weight="regular" />
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold">{title}</h3>
          <p className="mt-1 break-words text-sm leading-6 text-rose-800">{error.message}</p>
          <Button className="mt-4" type="button" onClick={onRetry} icon={<ArrowClockwise size={16} weight="regular" />}>
            Try again
          </Button>
        </div>
      </div>
    </div>
  );
}

export function InlineNotice({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "success" | "warning" | "danger";
  title: string;
  children?: ReactNode;
}) {
  const toneClass = {
    info: "border-sky-200 bg-sky-50 text-sky-900",
    success: "border-emerald-200 bg-emerald-50 text-emerald-900",
    warning: "border-amber-200 bg-amber-50 text-amber-950",
    danger: "border-rose-200 bg-rose-50 text-rose-950",
  }[tone];
  const Icon = tone === "success" ? CheckCircle : tone === "danger" ? XCircle : tone === "warning" ? WarningCircle : Info;
  return (
    <div className={`flex items-start gap-3 rounded-lg border p-4 ${toneClass}`}>
      <Icon size={19} weight="regular" className="mt-0.5 shrink-0" />
      <div>
        <p className="text-sm font-semibold">{title}</p>
        {children && <div className="mt-1 text-sm leading-6 opacity-90">{children}</div>}
      </div>
    </div>
  );
}

export function Metric({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail?: string; tone?: "neutral" | "good" | "bad" }) {
  const valueClass = tone === "good" ? "text-emerald-700" : tone === "bad" ? "text-rose-700" : "text-zinc-950";
  return (
    <div className="min-w-0 bg-white p-5">
      <p className="text-xs font-medium uppercase tracking-[0.11em] text-slate-500">{label}</p>
      <p className={`mt-3 font-mono text-3xl font-medium tracking-tight ${valueClass}`}>{value}</p>
      {detail && <p className="mt-2 truncate text-xs text-slate-500">{detail}</p>}
    </div>
  );
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const safe = Math.max(0, Math.min(100, value));
  return (
    <div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-200">
        <div className="h-full origin-left rounded-full bg-emerald-600 transition-transform duration-500" style={{ transform: `scaleX(${safe / 100})` }} />
      </div>
      {label && <p className="mt-2 font-mono text-xs text-slate-500">{label}</p>}
    </div>
  );
}

export function TableShell({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] border-collapse text-left text-sm" aria-label={label}>
          {children}
        </table>
      </div>
    </div>
  );
}

export const tableHeaderClass = "border-b border-slate-200 bg-slate-50/80 px-4 py-3 text-[11px] font-semibold uppercase tracking-[0.1em] text-slate-500";
export const tableCellClass = "border-b border-slate-100 px-4 py-3.5 align-top text-slate-700 last:border-b-0";
