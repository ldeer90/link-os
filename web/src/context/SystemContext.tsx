import { createContext, ReactNode, useContext, useEffect, useMemo, useState } from "react";
import { apiRequestFirst } from "../lib/api";
import { SystemHealth } from "../types";

interface SystemContextValue {
  health: SystemHealth | null;
  loading: boolean;
  error: Error | null;
  actionPending: boolean;
  reload: () => Promise<void>;
  pause: (reason?: string) => Promise<void>;
  resume: () => Promise<void>;
}

const SystemContext = createContext<SystemContextValue | null>(null);

function normalizeHealth(payload: unknown): SystemHealth {
  if (payload && typeof payload === "object" && "data" in payload) {
    return (payload as { data: SystemHealth }).data;
  }
  return (payload ?? {}) as SystemHealth;
}

export function SystemProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [actionPending, setActionPending] = useState(false);

  async function reload() {
    setError(null);
    try {
      const payload = await apiRequestFirst<SystemHealth>(["/health", "/system/health"]);
      setHealth(normalizeHealth(payload));
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error("System health is unavailable."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void reload();
    const interval = window.setInterval(() => void reload(), 60_000);
    return () => window.clearInterval(interval);
  }, []);

  async function pause(reason = "Paused from the LINK OS console") {
    setActionPending(true);
    try {
      const payload = await apiRequestFirst<SystemHealth>(["/outreach/pause", "/system/outreach/pause"], {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      const next = normalizeHealth(payload);
      setHealth((current) => ({ ...current, ...next, outreach_paused: true, pause_reason: next.pause_reason ?? reason }));
    } finally {
      setActionPending(false);
    }
  }

  async function resume() {
    setActionPending(true);
    try {
      const payload = await apiRequestFirst<SystemHealth>(["/outreach/resume", "/system/outreach/resume"], {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      const next = normalizeHealth(payload);
      setHealth((current) => ({ ...current, ...next, outreach_paused: false, pause_reason: null }));
    } finally {
      setActionPending(false);
    }
  }

  const value = useMemo(
    () => ({ health, loading, error, actionPending, reload, pause, resume }),
    [health, loading, error, actionPending],
  );

  return <SystemContext.Provider value={value}>{children}</SystemContext.Provider>;
}

export function useSystem() {
  const context = useContext(SystemContext);
  if (!context) throw new Error("useSystem must be used inside SystemProvider");
  return context;
}
