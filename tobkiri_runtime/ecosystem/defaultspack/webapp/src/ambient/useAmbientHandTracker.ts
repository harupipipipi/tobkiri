import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import type { PinchState } from "./gesturePinchDetector";
import { startHandLandmarkerLoop, type HandTrackingFrame } from "./mediaPipeHandLandmarker";

type UseAmbientHandTrackerOptions = {
  cameraStream: MediaStream | null;
  monitorEnabled: boolean;
  onPinchState: (state: PinchState) => void;
  rumiApprovalPending: boolean;
  setMessage: Dispatch<SetStateAction<string | null>>;
  setPinchDetectorStatus: Dispatch<SetStateAction<string>>;
  setTrackingFrame: Dispatch<SetStateAction<HandTrackingFrame | null>>;
  videoElement: HTMLVideoElement | null;
};

export function useAmbientHandTracker({
  cameraStream,
  monitorEnabled,
  onPinchState,
  rumiApprovalPending,
  setMessage,
  setPinchDetectorStatus,
  setTrackingFrame,
  videoElement,
}: UseAmbientHandTrackerOptions) {
  const gestureStopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let cancelled = false;
    gestureStopRef.current?.();
    gestureStopRef.current = null;
    if (rumiApprovalPending || !monitorEnabled || !cameraStream || !videoElement) {
      setPinchDetectorStatus(cameraStream ? "paused" : "idle");
      setTrackingFrame(null);
      return;
    }
    setPinchDetectorStatus("loading");
    startHandLandmarkerLoop(videoElement, onPinchState, {
      choiceRequiresPinch: true,
      pinchStartMs: 250,
      pinchReleaseMs: 180,
      onFrame: (frame) => setTrackingFrame(frame),
      onError: (error) => {
        if (cancelled) return;
        setPinchDetectorStatus("unavailable");
        setTrackingFrame(null);
        setMessage(error instanceof Error ? error.message : "手の認識が停止しました。");
      },
    })
      .then((stop) => {
        if (cancelled) {
          stop();
          return;
        }
        gestureStopRef.current = stop;
        setPinchDetectorStatus("tracking");
      })
      .catch((error) => {
        if (!cancelled) {
          setPinchDetectorStatus("unavailable");
          setMessage(error instanceof Error ? error.message : "指の検出を開始できませんでした。");
        }
      });
    return () => {
      cancelled = true;
      gestureStopRef.current?.();
      gestureStopRef.current = null;
      setTrackingFrame(null);
    };
  }, [
    cameraStream,
    monitorEnabled,
    onPinchState,
    rumiApprovalPending,
    setMessage,
    setPinchDetectorStatus,
    setTrackingFrame,
    videoElement,
  ]);
}
