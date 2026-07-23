import { useEffect, useState } from "react";
import { apiEventUrl } from "../lib/api";

type StreamState = "connecting" | "live" | "offline";
const scrapeEventNames = ["scrape_started", "page_started", "page_crawled", "email_found", "scrape_failed", "scrape_completed"] as const;

export function useLiveEvents(onEvent?: (payload: unknown) => void): StreamState {
  const [state, setState] = useState<StreamState>("connecting");

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setState("offline");
      return;
    }

    const source = new EventSource(apiEventUrl);
    source.onopen = () => setState("live");
    source.onerror = () => setState("offline");
    const receiveUpdate = (event: MessageEvent<string>) => {
      let payload: unknown = event.data;
      try {
        payload = JSON.parse(event.data);
      } catch { /* Retain plain-text event data. */ }
      onEvent?.(payload);
      window.dispatchEvent(new CustomEvent("linkos:update", { detail: payload }));
    };
    const receiveScrapeEvent = (event: MessageEvent<string>) => {
      let payload: unknown = event.data;
      try {
        payload = JSON.parse(event.data);
      } catch { /* Retain plain-text event data. */ }
      onEvent?.(payload);
      window.dispatchEvent(new CustomEvent("linkos:scrape", { detail: payload }));
      window.dispatchEvent(new CustomEvent("linkos:update", { detail: payload }));
    };
    const receiveHeartbeat = () => setState("live");
    source.onmessage = receiveUpdate;
    source.addEventListener("update", receiveUpdate as EventListener);
    scrapeEventNames.forEach((eventName) => source.addEventListener(eventName, receiveScrapeEvent as EventListener));
    source.addEventListener("heartbeat", receiveHeartbeat);

    return () => {
      source.removeEventListener("update", receiveUpdate as EventListener);
      scrapeEventNames.forEach((eventName) => source.removeEventListener(eventName, receiveScrapeEvent as EventListener));
      source.removeEventListener("heartbeat", receiveHeartbeat);
      source.close();
    };
  }, [onEvent]);

  return state;
}
