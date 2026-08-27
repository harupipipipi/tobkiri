import { useEffect, useRef, useState } from "react";

export type AdaptiveResourceStatus = "placeholder" | "loading" | "live" | "error";

export type AdaptiveResource<T> = {
  data: T | null;
  status: AdaptiveResourceStatus;
  error: string | null;
  refresh: () => void;
  updateData: (update: T | ((current: T | null) => T | null)) => void;
};

export function useAdaptiveResource<T>({
  initialData,
  load,
  enabled = true,
}: {
  demoData: T;
  initialData?: T;
  load: () => Promise<T>;
  enabled?: boolean;
}): AdaptiveResource<T> {
  const [data, setData] = useState<T | null>(initialData ?? null);
  const [status, setStatus] = useState<AdaptiveResourceStatus>(initialData ? "live" : enabled ? "loading" : "placeholder");
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const hasLiveDataRef = useRef(Boolean(initialData));

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setStatus((current) => (current === "live" ? "live" : "loading"));
    setError(null);

    void load()
      .then((nextData) => {
        if (cancelled) return;
        hasLiveDataRef.current = true;
        setData(nextData);
        setStatus("live");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (!hasLiveDataRef.current) {
          setData(null);
        }
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, load, nonce]);

  return {
    data,
    status,
    error,
    refresh: () => setNonce((value) => value + 1),
    updateData: (update) => {
      setData((current) => (typeof update === "function"
        ? (update as (value: T | null) => T | null)(current)
        : update));
      hasLiveDataRef.current = true;
      setStatus("live");
    },
  };
}
