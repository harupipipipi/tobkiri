import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import { DesktopTile } from "../src/components/desktops/DesktopTile";
import type { DesktopInputAction, DesktopInstance } from "../src/features/sandboxes/types";
import "../src/index.css";

type HarnessInput = DesktopInputAction & { seat_id: string };

type DesktopCaptureHarness = {
  clearInputs: () => void;
  getInputs: () => HarnessInput[];
  setLeaseSeat: (seatId: string | null) => void;
  setRejectInput: (reject: boolean) => void;
  setStatus: (seatId: string, status: DesktopInstance["status"]) => void;
};

declare global {
  interface Window {
    desktopCaptureHarness?: DesktopCaptureHarness;
  }
}

const desktop = (seatId: string): DesktopInstance => ({
  seat_id: seatId,
  name: seatId === "seat-1" ? "Primary desktop" : "Secondary desktop",
  status: "running",
  provider_id: "contract-harness",
  resolution: { width: 1024, height: 768 },
});

function DesktopKeyboardCaptureHarness() {
  const [desktops, setDesktops] = useState(() => [desktop("seat-1"), desktop("seat-2")]);
  const [leaseSeatId, setLeaseSeatId] = useState<string | null>("seat-1");
  const [rejectInput, setRejectInput] = useState(false);
  const inputsRef = useRef<HarnessInput[]>([]);

  useEffect(() => {
    window.desktopCaptureHarness = {
      clearInputs: () => {
        inputsRef.current = [];
      },
      getInputs: () => [...inputsRef.current],
      setLeaseSeat: setLeaseSeatId,
      setRejectInput,
      setStatus: (seatId, status) => {
        setDesktops((current) => current.map((item) => (
          item.seat_id === seatId ? { ...item, status } : item
        )));
      },
    };
    return () => {
      delete window.desktopCaptureHarness;
    };
  }, []);

  const onInput = async (seatId: string, input: DesktopInputAction): Promise<boolean> => {
    await Promise.resolve();
    if (rejectInput) return false;
    inputsRef.current = [...inputsRef.current, { ...input, seat_id: seatId }];
    return true;
  };

  return (
    <main
      className="grid min-h-screen gap-3 bg-zinc-950 p-3 min-[900px]:grid-cols-2"
      data-testid="desktop-capture-harness"
      data-lease-seat={leaseSeatId ?? "none"}
      data-reject-input={String(rejectInput)}
    >
      {desktops.map((item) => (
        <DesktopTile
          key={item.seat_id}
          desktop={item}
          selected={item.seat_id === "seat-1"}
          hasLease={leaseSeatId === item.seat_id}
          onSelect={() => undefined}
          onTakeOver={() => setLeaseSeatId(item.seat_id)}
          onReturnToAI={() => setLeaseSeatId(null)}
          onInput={(input) => onInput(item.seat_id, input)}
          onStart={() => undefined}
          onRestart={() => undefined}
          onStop={() => undefined}
          onDelete={() => undefined}
        />
      ))}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <DesktopKeyboardCaptureHarness />
  </StrictMode>,
);
