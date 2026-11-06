import { useCallback, useEffect, useRef, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  lastSuccessAt: number | null;
  loading: boolean;
  failures: number;
}

/**
 * Bounded HTTP polling: aborts the in-flight request when inputs change, backs off on errors,
 * slows down for hidden tabs, and never runs concurrent requests for the same key.
 */
export function usePolling<T>(
  fetcher: ((signal: AbortSignal) => Promise<T>) | null,
  intervalMs: number,
  key: string,
  enabled = true,
): PollState<T> & { refresh: () => void } {
  const [state, setState] = useState<PollState<T>>({
    data: null,
    error: null,
    lastSuccessAt: null,
    loading: true,
    failures: 0,
  });
  const [tick, setTick] = useState(0);
  const failures = useRef(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    // a new key means a new question: drop the old answer so it is never shown for the new one
    setState({ data: null, error: null, lastSuccessAt: null, loading: true, failures: 0 });
    failures.current = 0;
  }, [key]);

  useEffect(() => {
    if (!fetcher) return;
    let cancelled = false;
    let timer: number | undefined;
    let ctrl: AbortController | null = null;

    const schedule = () => {
      if (cancelled || !enabled) return;
      const hidden = document.visibilityState === "hidden";
      const base = hidden ? Math.max(intervalMs * 6, 30000) : intervalMs;
      const backoff = Math.min(60000, base * 2 ** Math.min(failures.current, 5));
      timer = window.setTimeout(run, failures.current ? backoff : base);
    };

    const run = async () => {
      ctrl?.abort();
      ctrl = new AbortController();
      const mine = ctrl;
      try {
        const data = await fetcher(mine.signal);
        if (cancelled || mine.signal.aborted) return;
        failures.current = 0;
        setState({ data, error: null, lastSuccessAt: Date.now(), loading: false, failures: 0 });
      } catch (err) {
        if (cancelled || mine.signal.aborted) return;
        failures.current += 1;
        setState((s) => ({ ...s, error: err as Error, loading: false, failures: failures.current }));
      }
      schedule();
    };

    const onVisible = () => {
      if (document.visibilityState === "visible") {
        window.clearTimeout(timer);
        void run();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    void run();
    return () => {
      cancelled = true;
      ctrl?.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, intervalMs, enabled, tick]);

  return { ...state, refresh };
}

export function useOnline(): boolean {
  const [online, setOnline] = useState(navigator.onLine);
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function useStoredState<T extends string>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      return (window.localStorage.getItem(key) as T | null) ?? initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T) => {
      setValue(v);
      try {
        window.localStorage.setItem(key, v);
      } catch {
        /* storage unavailable: keep the in-memory value */
      }
    },
    [key],
  );
  return [value, set];
}
