import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { sandboxesApi } from "./api";
import { normalizeDesktopStatus } from "./types";
import type { DesktopFrameQuality, DesktopFrameResult, DesktopFrameView, DesktopStatus, SandboxState } from "./types";

export type DesktopFrameFetcher = (
  seatId: string,
  options: {
    afterSeq?: number | null;
    quality?: DesktopFrameQuality;
    accessKey?: string | null;
    signal?: AbortSignal;
  },
) => Promise<DesktopFrameResult>;

export type DesktopFramePollResult =
  | { status: "frame"; frameSeq: number }
  | { status: "not_modified" }
  | { status: "skipped"; reason: "in_flight" }
  | { status: "aborted" }
  | { status: "error"; error: unknown };

export class DesktopFramePoller {
  private inFlight: AbortController | null = null;
  private lastSeq: number | null;
  private readonly seatId: string;
  private readonly quality: DesktopFrameQuality;
  private readonly fetcher: DesktopFrameFetcher;
  private readonly accessKey?: string | null;
  private readonly onFrame?: (frame: Extract<DesktopFrameResult, { status: "frame" }>) => void;
  private readonly onError?: (error: unknown) => void;

  constructor(options: {
    seatId: string;
    quality: DesktopFrameQuality;
    accessKey?: string | null;
    fetcher: DesktopFrameFetcher;
    initialSeq?: number | null;
    onFrame?: (frame: Extract<DesktopFrameResult, { status: "frame" }>) => void;
    onError?: (error: unknown) => void;
  }) {
    this.seatId = options.seatId;
    this.quality = options.quality;
    this.accessKey = options.accessKey;
    this.fetcher = options.fetcher;
    this.lastSeq = options.initialSeq ?? null;
    this.onFrame = options.onFrame;
    this.onError = options.onError;
  }

  getLastSeq() {
    return this.lastSeq;
  }

  hasInFlightRequest() {
    return Boolean(this.inFlight);
  }

  abort() {
    this.inFlight?.abort();
  }

  async pollOnce(): Promise<DesktopFramePollResult> {
    if (this.inFlight) return { status: "skipped", reason: "in_flight" };

    const controller = new AbortController();
    this.inFlight = controller;
    try {
      const result = await this.fetcher(this.seatId, {
        afterSeq: this.lastSeq,
        quality: this.quality,
        accessKey: this.accessKey,
        signal: controller.signal,
      });
      if (result.status === "frame") {
        this.lastSeq = result.frame_seq;
        this.onFrame?.(result);
        return { status: "frame", frameSeq: result.frame_seq };
      }
      return { status: "not_modified" };
    } catch (error) {
      if (controller.signal.aborted) return { status: "aborted" };
      this.onError?.(error);
      return { status: "error", error };
    } finally {
      if (this.inFlight === controller) this.inFlight = null;
    }
  }
}

function isDocumentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

type DesktopFrameRuntimeStatus = DesktopStatus | SandboxState | undefined;

export function isRunningStatus(status: DesktopFrameRuntimeStatus): boolean {
  return normalizeDesktopStatus(status) === "running";
}

function cadenceFor(options: { selected: boolean; hasLease: boolean }): number {
  if (options.hasLease) return 250;
  if (options.selected) return 500;
  return 1200;
}

export function useDesktopFrame({
  seatId,
  enabled = true,
  status,
  selected,
  hasControlLease,
  accessKey,
  fetcher = sandboxesApi.fetchDesktopFrame,
}: {
  seatId: string;
  enabled?: boolean;
  status?: DesktopFrameRuntimeStatus;
  selected: boolean;
  hasControlLease: boolean;
  accessKey?: string | null;
  fetcher?: DesktopFrameFetcher;
}) {
  const [frame, setFrame] = useState<DesktopFrameView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPolling, setIsPolling] = useState(false);
  const objectUrlRef = useRef<string | null>(null);
  const backoffRef = useRef(1000);
  const pollerRef = useRef<DesktopFramePoller | null>(null);
  const quality: DesktopFrameQuality = hasControlLease ? "control" : selected ? "focus" : "grid";

  const releaseObjectUrl = useCallback((url: string | null) => {
    if (!url || typeof URL === "undefined" || typeof URL.revokeObjectURL !== "function") return;
    URL.revokeObjectURL(url);
  }, []);

  const handleFrame = useCallback((nextFrame: Extract<DesktopFrameResult, { status: "frame" }>) => {
    if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return;
    const objectUrl = URL.createObjectURL(nextFrame.blob);
    const previousUrl = objectUrlRef.current;
    objectUrlRef.current = objectUrl;
    setFrame({
      frame_seq: nextFrame.frame_seq,
      width: nextFrame.width,
      height: nextFrame.height,
      mime_type: nextFrame.mime_type,
      object_url: objectUrl,
      captured_at: nextFrame.captured_at,
      received_at: Date.now(),
    });
    releaseObjectUrl(previousUrl);
    backoffRef.current = 1000;
    setError(null);
  }, [releaseObjectUrl]);

  const pollNow = useCallback(async () => {
    if (!enabled || !isRunningStatus(status) || isDocumentHidden()) return;
    if (!pollerRef.current) {
      pollerRef.current = new DesktopFramePoller({
        seatId,
        quality,
        accessKey,
        fetcher,
        initialSeq: frame?.frame_seq ?? null,
        onFrame: handleFrame,
        onError: (pollError) => {
          setError(pollError instanceof Error ? pollError.message : "Desktop frame polling failed.");
          backoffRef.current = Math.min(backoffRef.current * 2, 8000);
        },
      });
    }
    setIsPolling(true);
    const result = await pollerRef.current.pollOnce();
    if (result.status === "not_modified") {
      backoffRef.current = 1000;
      setError(null);
    }
    setIsPolling(false);
  }, [enabled, accessKey, fetcher, frame?.frame_seq, handleFrame, quality, seatId, status]);

  useEffect(() => {
    pollerRef.current?.abort();
    pollerRef.current = null;
  }, [enabled, accessKey, fetcher, quality, seatId]);

  useEffect(() => {
    if (!enabled || !isRunningStatus(status)) return;
    let cancelled = false;
    let timer: number | null = null;
    const schedule = (delay: number) => {
      timer = window.setTimeout(() => {
        if (cancelled) return;
        if (isDocumentHidden()) {
          schedule(1000);
          return;
        }
        void pollNow().finally(() => {
          if (!cancelled) schedule(error ? backoffRef.current : cadenceFor({ selected, hasLease: hasControlLease }));
        });
      }, delay);
    };
    void pollNow();
    schedule(cadenceFor({ selected, hasLease: hasControlLease }));

    const handleVisibility = () => {
      if (isDocumentHidden()) {
        pollerRef.current?.abort();
      } else {
        void pollNow();
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
      pollerRef.current?.abort();
    };
  }, [enabled, error, hasControlLease, pollNow, selected, status]);

  useEffect(() => {
    return () => {
      pollerRef.current?.abort();
      releaseObjectUrl(objectUrlRef.current);
      objectUrlRef.current = null;
    };
  }, [releaseObjectUrl]);

  const ageMs = useMemo(() => frame ? Date.now() - frame.received_at : null, [frame]);

  return {
    frame,
    error,
    isPolling,
    ageMs,
    pollNow,
  };
}
