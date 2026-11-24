import { useEffect, useState } from "react";

/** Keep a closing surface mounted until its exit animation finishes. */
export function useExitPresence(visible: boolean, duration: number): boolean {
  const [retained, setRetained] = useState(visible);
  useEffect(() => {
    if (visible) {
      setRetained(true);
      return;
    }
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = window.setTimeout(() => setRetained(false), reducedMotion ? 0 : duration);
    return () => window.clearTimeout(timer);
  }, [visible, duration]);
  return visible || retained;
}
