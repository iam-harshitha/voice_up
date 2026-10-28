import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, isAbort } from "../api/client";

export interface PollState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** True when the last refresh failed but older data is still shown. */
  stale: boolean;
  lastUpdated: Date | null;
  refresh: () => void;
}

interface Options {
  /** Milliseconds between refreshes; null loads once. */
  intervalMs: number | null;
  /** When false, nothing is fetched (e.g. missing permission). */
  enabled?: boolean;
}

/**
 * Loads data and optionally refreshes it on an interval.
 * - Never runs two requests at once (a new one waits for the previous to finish).
 * - Pauses while the browser tab is hidden and refreshes when it becomes visible.
 * - Cancels the in-flight request on unmount or when the key changes.
 * - Keeps the last good data on failure and marks it stale instead of clearing it.
 */
export function usePolling<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
  { intervalMs, enabled = true }: Options,
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const inFlight = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  // Reset when the resource changes.
  useEffect(() => {
    setData(null);
    setError(null);
    setLastUpdated(null);
  }, [key]);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let cancelled = false;

    const schedule = () => {
      if (cancelled || intervalMs === null) return;
      timer.current = setTimeout(run, intervalMs);
    };

    async function run() {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        schedule();
        return;
      }
      if (inFlight.current) return; // never overlap
      const controller = new AbortController();
      inFlight.current = controller;
      setLoading(true);
      try {
        const result = await loadRef.current(controller.signal);
        if (cancelled) return;
        setData(result);
        setError(null);
        setLastUpdated(new Date());
      } catch (caught) {
        if (cancelled || isAbort(caught)) return;
        setError(caught instanceof ApiError ? caught : new ApiError(0, "client_error", String(caught)));
      } finally {
        if (inFlight.current === controller) inFlight.current = null;
        if (!cancelled) setLoading(false);
      }
      schedule();
    }

    const onVisible = () => {
      if (document.visibilityState === "visible" && intervalMs !== null) {
        if (timer.current) clearTimeout(timer.current);
        void run();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    void run();
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
      if (timer.current) clearTimeout(timer.current);
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, [key, intervalMs, enabled, tick]);

  return { data, error, loading, stale: Boolean(error && data), lastUpdated, refresh };
}
