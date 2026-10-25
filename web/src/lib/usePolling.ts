import { useCallback, useEffect, useRef, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** Fetch now (e.g. after a mutation) without waiting for the next tick. */
  refresh: () => void;
}

/**
 * Calls `load` immediately and then every `intervalMs` while enabled. Polling
 * pauses while the tab is hidden, so a forgotten tab does not keep the gateway
 * (and its idle timer, which decides scale-to-zero) busy.
 */
export function usePolling<T>(
  load: (signal: AbortSignal) => Promise<T>,
  intervalMs: number | null,
  deps: readonly unknown[] = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let stopped = false;

    const run = async () => {
      if (document.visibilityState === "hidden" && intervalMs !== null) {
        schedule();
        return;
      }
      try {
        const result = await loadRef.current(controller.signal);
        if (stopped) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (stopped || (err instanceof DOMException && err.name === "AbortError")) return;
        setError(err);
      } finally {
        if (!stopped) setLoading(false);
      }
      schedule();
    };

    const schedule = () => {
      if (stopped || intervalMs === null) return;
      timer = window.setTimeout(run, intervalMs);
    };

    const onVisible = () => {
      if (document.visibilityState === "visible" && intervalMs !== null) {
        window.clearTimeout(timer);
        void run();
      }
    };

    void run();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs, tick, ...deps]);

  return { data, error, loading, refresh };
}
