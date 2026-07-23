import { DependencyList, useCallback, useEffect, useState } from "react";

export interface ApiResource<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  reload: () => Promise<void>;
}

export function useApi<T>(loader: () => Promise<T>, dependencies: DependencyList = []): ApiResource<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await loader());
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error("The service did not return a usable response."));
    } finally {
      setLoading(false);
    }
  }, dependencies); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    let refreshTimer: number | undefined;
    const refreshSoon = () => {
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => void reload(), 250);
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refreshSoon();
    };
    window.addEventListener("linkos:update", refreshSoon);
    window.addEventListener("focus", refreshSoon);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
      window.removeEventListener("linkos:update", refreshSoon);
      window.removeEventListener("focus", refreshSoon);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [reload]);

  return { data, loading, error, reload };
}
