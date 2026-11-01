import test from "node:test";
import assert from "node:assert/strict";
import fixtures from "./templateToolPolicyMerge.fixtures.json" with { type: "json" };
import type { TemplateAiInput, TemplateToolPolicy } from "./api";
import {
  materializedTemplateToolPolicySettings,
  templateToolPolicySettings,
} from "./templateToolPolicyMerge";
import { templateToolPolicyReferencePayload } from "./templateAiInput";

const AUTHORITY_KEYS = new Set([
  "composed_tool_policy_id",
  "template_tool_policy_id",
  "template_tool_policy_ids",
  "template_tool_policy_projected_id",
  "template_tool_policy_projected_ids",
]);

function semanticPolicy(policy: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(policy).filter(([key]) => !AUTHORITY_KEYS.has(key)));
}

test("template tool policy merge shared fixture cases", () => {
  for (const fixture of fixtures) {
    const settings = templateToolPolicySettings(fixture.policies as TemplateToolPolicy[], {
      requestDisabledTools: fixture.request_disabled_tools,
    });
    const policy = semanticPolicy(materializedTemplateToolPolicySettings(settings));
    assert.deepEqual(policy, fixture.expected_policy ?? {}, fixture.name);
    const diagnosticCodes = settings.diagnostics.map((item) => item.code);
    assert.deepEqual(diagnosticCodes, fixture.expected_diagnostic_codes ?? [], fixture.name);
    if (fixture.expected_source_ids) {
      assert.deepEqual(settings.ids, fixture.expected_source_ids, fixture.name);
    }
    if (fixture.expected_projected_ids) {
      assert.deepEqual(settings.projectedIds, fixture.expected_projected_ids, fixture.name);
    }
  }
});

test("template tool policy reference payload sends real source ids only", () => {
  const aiInput = {
    id: "composed_ai_input:first+second",
    metadata: { source_ids: ["first_ai", "second_ai"] },
  } as TemplateAiInput;
  const policy = {
    id: "composed_tool_policy:abc123",
    metadata: { source_ids: ["first_policy", "second_policy"] },
  } as TemplateToolPolicy;

  assert.deepEqual(templateToolPolicyReferencePayload(aiInput, policy), {
    template_ai_input_ids: ["first_ai", "second_ai"],
    template_tool_policy_ids: ["first_policy", "second_policy"],
  });
});
