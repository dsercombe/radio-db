import { useEffect, useRef } from "react";

interface VisibilityPollingOptions {
  intervalMs: number;
  initialDelayMs?: number;
  backoffFactor?: number;
  maxIntervalMs?: number;
  enabled?: boolean;
}

export function useVisibilityPolling(
  task: () => Promise<void> | void,
  {
    intervalMs,
    initialDelayMs = 0,
    backoffFactor = 2,
    maxIntervalMs,
    enabled = true,
  }: VisibilityPollingOptions
): void {
  const timerRef = useRef<number | null>(null);
  const runningRef = useRef(false);
  const taskRef = useRef(task);
  const intervalRef = useRef(intervalMs);
  const pausedRef = useRef(false);

  useEffect(() => {
    taskRef.current = task;
  }, [task]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let disposed = false;
    const maxInterval = maxIntervalMs ?? Math.max(intervalMs * 4, intervalMs);

    const clearTimer = () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const schedule = (delay: number) => {
      if (disposed) {
        return;
      }
      clearTimer();
      timerRef.current = window.setTimeout(() => {
        void run();
      }, delay);
    };

    const run = async () => {
      if (disposed || runningRef.current) {
        return;
      }
      if (document.visibilityState === "hidden") {
        pausedRef.current = true;
        return;
      }
      runningRef.current = true;
      try {
        await taskRef.current();
        intervalRef.current = intervalMs;
      } catch {
        intervalRef.current = Math.min(maxInterval, Math.max(intervalMs, intervalRef.current * backoffFactor));
      } finally {
        runningRef.current = false;
        if (!disposed && document.visibilityState === "visible") {
          schedule(intervalRef.current);
        }
      }
    };

    const handleVisibility = () => {
      if (disposed) {
        return;
      }
      if (document.visibilityState === "visible") {
        pausedRef.current = false;
        intervalRef.current = intervalMs;
        schedule(0);
      } else {
        pausedRef.current = true;
        clearTimer();
      }
    };

    schedule(initialDelayMs);
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      disposed = true;
      clearTimer();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [enabled, intervalMs, initialDelayMs, backoffFactor, maxIntervalMs]);
}
