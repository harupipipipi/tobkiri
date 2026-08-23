import React from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import {
  SURFACE_TEMPLATE_API_VERSION,
  SurfaceTemplateRenderer,
  type SurfaceTemplate,
} from "../src/surface/SurfaceTemplateRenderer";

const contentTemplate: SurfaceTemplate = {
  surface_api_version: SURFACE_TEMPLATE_API_VERSION,
  template_id: "test.browser.content",
  version: "1.0.0",
  input: { pattern: "form" },
  outcomes: {
    success: { pattern: "content", data: "$.result" },
  },
  actions: [{
    contract_id: "test.browser",
    operation_id: "continue",
    payload_binding: { choice: "$.choice" },
    label: "Continue safely",
  }],
};

const confirmationTemplate: SurfaceTemplate = {
  surface_api_version: SURFACE_TEMPLATE_API_VERSION,
  template_id: "test.browser.confirmation",
  version: "1.0.0",
  input: { pattern: "confirmation" },
  outcomes: {
    success: { pattern: "confirmation", message: "$.message" },
  },
  security: { class: "sensitive", requires_trusted_renderer: true },
  actions: [{
    contract_id: "test.browser",
    operation_id: "delete",
    payload_binding: {},
    label: "Delete",
    sensitive: true,
  }],
};

const dangerousText = `<img src=x onerror="document.body.dataset.compromised='yes'">${"unbroken".repeat(80)}`;

function Fixture() {
  return (
    <div className="w-full max-w-full p-2" data-browser-fixture-ready>
      <div data-testid="content-surface">
        <SurfaceTemplateRenderer
          template={contentTemplate}
          event={{ kind: "success", result: dangerousText, choice: "approved" }}
          onAction={(_action, payload) => {
            document.querySelector("[data-testid=action-result]")!.textContent = JSON.stringify(payload);
          }}
        />
      </div>
      <output data-testid="action-result" aria-live="polite" />
      <div data-testid="untrusted-surface">
        <SurfaceTemplateRenderer
          template={confirmationTemplate}
          event={{ kind: "success", message: "Delete everything?" }}
        />
      </div>
      <div data-testid="failed-surface">
        <SurfaceTemplateRenderer
          template={{ ...contentTemplate, outcomes: undefined } as unknown as SurfaceTemplate}
          event={{ kind: "success" }}
        />
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
