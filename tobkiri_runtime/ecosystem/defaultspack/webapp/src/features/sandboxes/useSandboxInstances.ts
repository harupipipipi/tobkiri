import { useCallback, useEffect, useRef, useState } from "react";

import { sandboxesApi } from "./api";
import type { DesktopInstance, SandboxInstance } from "./types";

type SandboxInstancesClient = Pick<typeof sandboxesApi, "listSandboxes" | "listDesktops">;

export function useSandboxInstances({
  enabled = true,
  client = sandboxesApi,
}: {
  enabled?: boolean;
  client?: Pick<SandboxInstancesClient, "listSandboxes">;
} = {}) {
  const [sandboxes, setSandboxes] = useState<SandboxInstance[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return [];
    setLoading(true);
    try {
      const result = await client.listSandboxes();
      setSandboxes(result.sandboxes);
      setError(null);
      return result.sandboxes;
    } catch (sandboxError) {
      setSandboxes([]);
      setError(sandboxError instanceof Error ? sandboxError.message : "Sandbox lookup failed.");
      return [];
    } finally {
      setLoading(false);
    }
  }, [client, enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { sandboxes, loading, error, refresh };
}

export function useDesktopInstances({
  enabled = true,
  client = sandboxesApi,
  pollIntervalMs = 0,
}: {
  enabled?: boolean;
  client?: Pick<SandboxInstancesClient, "listDesktops">;
  pollIntervalMs?: number;
} = {}) {
  const [desktops, setDesktops] = useState<DesktopInstance[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const refreshInFlightRef = useRef(false);
  const desktopsRef = useRef<DesktopInstance[]>([]);

  const refresh = useCallback(async (options: { silent?: boolean; throwOnError?: boolean } = {}) => {
    if (!enabled) return [];
    if (refreshInFlightRef.current) {
      if (!options.throwOnError) return desktopsRef.current;
      try {
        const result = await client.listDesktops();
        desktopsRef.current = result.desktops;
        setDesktops(result.desktops);
        setError(null);
        return result.desktops;
      } catch (desktopError) {
        setError(desktopError instanceof Error ? desktopError.message : "Desktop lookup failed.");
        throw desktopError;
      }
    }
    refreshInFlightRef.current = true;
    if (!options.silent) setLoading(true);
    try {
      const result = await client.listDesktops();
      desktopsRef.current = result.desktops;
      setDesktops(result.desktops);
      setError(null);
      return result.desktops;
    } catch (desktopError) {
      setDesktops(desktopsRef.current);
      setError(desktopError instanceof Error ? desktopError.message : "Desktop lookup failed.");
      if (options.throwOnError) throw desktopError;
      return desktopsRef.current;
    } finally {
      refreshInFlightRef.current = false;
      if (!options.silent) setLoading(false);
    }
  }, [client, enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!enabled || pollIntervalMs <= 0) return;
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void refresh({ silent: true });
    }, pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, pollIntervalMs, refresh]);

  return { desktops, loading, error, refresh, setDesktops };
}
