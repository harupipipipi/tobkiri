import { createRoot } from "react-dom/client";
import { ModelRouteSetup } from "../src/features/models/ModelRouteSetup";

// Mount the production component alone; no runtime route or Host policy changes.
export function mountModelRouteSetup() {
  createRoot(document.getElementById("model-route-root")!).render(<ModelRouteSetup />);
}
