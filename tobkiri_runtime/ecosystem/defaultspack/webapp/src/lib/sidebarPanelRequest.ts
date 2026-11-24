/** Canvas mirror requests are events, not continuously enforced panel values.
 * Other panels retain their existing catalogue/readiness reconciliation behavior.
 */
export function consumeSidebarCanvasRequest(
  consumed: { current: string | null },
  token: string | null | undefined,
  panelId: string | null,
  canvasAvailable: boolean,
): boolean {
  if (panelId !== "__canvas_widget__" && panelId !== "__close_canvas_widget__") {
    consumed.current = null;
    return true;
  }
  if (!token || (panelId === "__canvas_widget__" && !canvasAvailable)
    || consumed.current === token) return false;
  consumed.current = token;
  return true;
}

/** The native Canvas stays independent even if the catalogue repeats its reserved ID. */
export function isAvailableSidebarCanvas(panelId: string | null, canvasAvailable: boolean): boolean {
  return panelId === "__canvas_widget__" && canvasAvailable;
}
