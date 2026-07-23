import {
  AddressBook,
  Buildings,
  ChatCircleText,
  Database,
  Gauge,
  Gear,
  GlobeHemisphereWest,
  Megaphone,
  PauseCircle,
  Queue,
  ShieldCheck,
  TreeStructure,
} from "@phosphor-icons/react";
import { NavLink, Outlet } from "react-router-dom";
import { useLiveEvents } from "../hooks/useLiveEvents";
import { useSystem } from "../context/SystemContext";
import { StatusBadge } from "./ui";

const navigation = [
  { label: "Overview", href: "/", icon: Gauge },
  { label: "Intake", href: "/intake", icon: Queue },
  { label: "Domains", href: "/domains", icon: GlobeHemisphereWest },
  { label: "Scraping", href: "/scraping", icon: Database },
  { label: "Backlink discovery", href: "/backlinks", icon: TreeStructure },
  { label: "Campaigns", href: "/campaigns", icon: Megaphone },
  { label: "Replies & offers", href: "/replies", icon: ChatCircleText },
  { label: "Inventory", href: "/inventory", icon: AddressBook },
  { label: "Agencies", href: "/agencies", icon: Buildings },
  { label: "Settings", href: "/settings", icon: Gear },
];

function BrandMark() {
  return (
    <div className="flex items-center gap-3">
      <span className="grid size-9 place-items-center rounded-lg bg-zinc-950 text-xs font-semibold tracking-[-0.04em] text-white shadow-sm">LO</span>
      <div>
        <p className="text-sm font-semibold tracking-tight text-zinc-950">LINK OS</p>
        <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">Operations console</p>
      </div>
    </div>
  );
}

export function AppShell() {
  const streamState = useLiveEvents();
  const { health, loading } = useSystem();
  const paused = health?.outreach_paused ?? true;

  return (
    <div className="min-h-[100dvh] text-zinc-900">
      <aside className="fixed inset-y-0 left-0 hidden w-64 border-r border-slate-200 bg-white/95 px-4 py-5 backdrop-blur-xl lg:flex lg:flex-col">
        <div className="px-2">
          <BrandMark />
        </div>
        <nav className="mt-8 space-y-1" aria-label="Primary navigation">
          {navigation.map(({ label, href, icon: Icon }) => (
            <NavLink
              end={href === "/"}
              key={href}
              to={href}
              className={({ isActive }) =>
                `focus-ring group flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition duration-200 active:translate-y-px ${
                  isActive ? "bg-zinc-950 text-white shadow-sm" : "text-slate-600 hover:bg-slate-100 hover:text-zinc-950"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  <Icon size={18} weight={isActive ? "fill" : "regular"} className={isActive ? "text-emerald-300" : "text-slate-400 group-hover:text-slate-600"} />
                  <span>{label}</span>
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto border-t border-slate-200 px-2 pt-5">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-xs text-slate-600">
              <span
                className={`size-2 rounded-full ${streamState === "live" ? "animate-breathe bg-emerald-500" : "bg-slate-300"}`}
                aria-hidden="true"
              />
              {streamState === "live" ? "Live updates" : "Polling mode"}
            </div>
            <StatusBadge status={paused ? "paused" : "active"} label={loading ? "Checking" : paused ? "Outreach paused" : "Outreach active"} />
          </div>
        </div>
      </aside>

      <div className="lg:pl-64">
        <header className="sticky top-0 border-b border-slate-200 bg-slate-50/90 backdrop-blur-xl lg:hidden">
          <div className="flex items-center justify-between px-4 py-3">
            <BrandMark />
            <StatusBadge status={paused ? "paused" : "active"} label={paused ? "Outreach paused" : "Outreach active"} />
          </div>
          <nav className="flex gap-1 overflow-x-auto border-t border-slate-200 px-3 py-2" aria-label="Mobile navigation">
            {navigation.map(({ label, href, icon: Icon }) => (
              <NavLink
                end={href === "/"}
                key={href}
                to={href}
                className={({ isActive }) =>
                  `focus-ring inline-flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium ${
                    isActive ? "bg-zinc-950 text-white" : "text-slate-600 hover:bg-white"
                  }`
                }
              >
                <Icon size={16} weight="regular" />
                {label}
              </NavLink>
            ))}
          </nav>
        </header>

        {paused && (
          <div className="border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-amber-950 md:px-8">
            <div className="mx-auto flex max-w-[1400px] items-center gap-2 text-xs font-medium md:text-sm">
              <PauseCircle size={17} weight="fill" className="shrink-0" />
              Outreach is paused. Scraping, reply sync and review remain available.
              {health?.pause_reason && <span className="hidden font-normal text-amber-800 md:inline">Reason: {health.pause_reason}</span>}
            </div>
          </div>
        )}

        <main className="mx-auto w-full max-w-[1400px] px-4 py-7 md:px-8 md:py-10">
          <Outlet />
        </main>
        <footer className="mx-auto flex max-w-[1400px] items-center justify-between border-t border-slate-200 px-4 py-6 text-xs text-slate-500 md:px-8 lg:ml-auto">
          <span>LINK OS local control plane</span>
          <span className="inline-flex items-center gap-1.5"><ShieldCheck size={15} weight="regular" /> Fail-closed outreach</span>
        </footer>
      </div>
    </div>
  );
}
