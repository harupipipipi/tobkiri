import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { sandboxesApi } from "./api";
import { runtimeAvailability, runtimeOperationSupportForMetadata, runSupportedRuntimeOperation, runtimeOperationAllowed } from "./runtimeStatus";
import type { RuntimeDoctorResult, RuntimeOperation, RuntimeProvidersResponse } from "./types";

type RuntimeDoctorClient = Pick<
  typeof sandboxesApi,
  "listRuntimeProviders" | "runRuntimeDoctor" | "ensureRuntime" | "getRuntimeOperation" | "cancelRuntimeOperation"
>;

export function useRuntimeDoctor({
  autoRunDoctor = false,
  client = sandboxesApi,
}: {
  autoRunDoctor?: boolean;
  client?: RuntimeDoctorClient;
} = {}) {
  const [providersResponse, setProvidersResponse] = useState<RuntimeProvidersResponse | null>(null);
  const [doctor, setDoctor] = useState<RuntimeDoctorResult | null>(null);
  const [operation, setOperation] = useState<RuntimeOperation | null>(null);
  const [loading, setLoading] = useState(true);
  const [doctorLoading, setDoctorLoading] = useState(false);
  const [setupLoading, setSetupLoading] = useState(false);
  const [operationCancelLoading, setOperationCancelLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const support = runtimeOperationSupportForMetadata(providersResponse, doctor);
  const doctorRef = useRef(doctor);
  doctorRef.current = doctor;
  const supportRef = useRef(support);
  supportRef.current = support;

  const refreshProviders = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const result = await client.listRuntimeProviders();
      if (signal?.aborted) return null;
      supportRef.current = runtimeOperationSupportForMetadata(result, doctorRef.current);
      setProvidersResponse(result);
      setError(null);
      return result;
    } catch (providerError) {
      if (signal?.aborted) return null;
      setProvidersResponse(null);
      setError(providerError instanceof Error ? providerError.message : "Runtime provider lookup failed.");
      return null;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [client]);

  const runDoctor = useCallback(async () => {
    setDoctorLoading(true);
    try {
      const result = await runSupportedRuntimeOperation(supportRef.current, "doctor", () => client.runRuntimeDoctor());
      setDoctor(result);
      setProvidersResponse((current) => result.providers ? { ...(current ?? { providers: [] }), providers: result.providers } : current);
      setError(null);
      return result;
    } catch (doctorError) {
      const message = doctorError instanceof Error ? doctorError.message : "Runtime doctor failed.";
      setError(message);
      return null;
    } finally {
      setDoctorLoading(false);
    }
  }, [client]);

  const ensureRuntime = useCallback(async (providerId?: string | null) => {
    setSetupLoading(true);
    try {
      const result = await runSupportedRuntimeOperation(supportRef.current, "setup", () => client.ensureRuntime(providerId));
      setOperation(result);
      setError(null);
      return result;
    } catch (setupError) {
      setError(setupError instanceof Error ? setupError.message : "Runtime setup failed to start.");
      return null;
    } finally {
      setSetupLoading(false);
    }
  }, [client]);

  const cancelRuntimeOperation = useCallback(async () => {
    if (!operation?.operation_id || ["completed", "failed", "cancelled"].includes(operation.status)) return null;
    setOperationCancelLoading(true);
    try {
      const result = await runSupportedRuntimeOperation(supportRef.current, "setup", () => client.cancelRuntimeOperation(operation.operation_id));
      setOperation(result);
      setError(null);
      return result;
    } catch (cancelError) {
      setError(cancelError instanceof Error ? cancelError.message : "Runtime operation cancellation failed.");
      return null;
    } finally {
      setOperationCancelLoading(false);
    }
  }, [client, operation?.operation_id, operation?.status]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshProviders(controller.signal).then((result) => {
      if (autoRunDoctor && result && runtimeOperationAllowed(result.operation_support, "doctor") && !controller.signal.aborted) {
        void runDoctor();
      }
    });
    return () => controller.abort();
  }, [autoRunDoctor, refreshProviders, runDoctor]);

  useEffect(() => {
    if (!operation?.operation_id) return;
    if (["completed", "failed", "cancelled"].includes(operation.status)) return;
    let cancelled = false;
    const interval = window.setInterval(() => {
      void client.getRuntimeOperation(operation.operation_id).then((nextOperation) => {
        if (cancelled) return;
        setOperation(nextOperation);
        if (nextOperation.status === "completed") {
          void refreshProviders();
          void runDoctor();
        }
      }).catch((operationError) => {
        if (!cancelled) setError(operationError instanceof Error ? operationError.message : "Runtime operation polling failed.");
      });
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [client, operation?.operation_id, operation?.status, refreshProviders, runDoctor]);

  const availability = useMemo(
    () => runtimeAvailability(providersResponse, doctor, error, loading || doctorLoading),
    [doctor, doctorLoading, error, loading, providersResponse],
  );

  return {
    operationSupport: support,
    availability,
    providersResponse,
    doctor,
    operation,
    loading,
    doctorLoading,
    setupLoading,
    operationCancelLoading,
    error,
    refreshProviders,
    runDoctor,
    ensureRuntime,
    cancelRuntimeOperation,
  };
}
