export type ExternalInputTemplate = Record<string, unknown>;

const FALLBACK_SETUP_STEPS: Record<string, string[]> = {
  line: [
    "Select LINE Input.",
    "Generate a public URL for /api/integrations/line/webhook.",
    "Paste it into the LINE Messaging API Webhook URL field.",
    "Save the Messaging API Channel Secret and Channel Access Token in External Tokens.",
    "Enable the line-main endpoint after checking the source rules.",
  ],
  discord: [
    "Select Discord Input.",
    "Use /api/integrations/discord/interactions for slash commands or /api/integrations/discord/events for message events.",
    "Save the Discord Public Key for interaction signature verification.",
    "Save a Bot Token only when Tobkiri should post to a bot channel.",
    "Enable the discord-main endpoint after checking the source rules.",
  ],
  slack: [
    "Select Slack Input.",
    "Generate a public URL for /api/integrations/slack/events.",
    "Paste it into the Slack Events Request URL field.",
    "Save the Signing Secret and Bot Token in External Tokens.",
    "Enable the slack-main endpoint after checking the source rules.",
  ],
  generic: [
    "Select Generic Webhook Input.",
    "Choose an endpoint ID and generate a public URL for /api/webhooks/inbound/{webhook_id}.",
    "Replace {webhook_id} with the configured endpoint ID before sharing the URL.",
    "Save a Webhook Shared Secret in External Tokens.",
    "Enable the endpoint after checking its source and delivery rules.",
  ],
};

const POLICY_SUMMARIES: Record<string, string> = {
  line: "line.production: verified LINE signatures and text messages required; saved sources allowed; unknown sources denied.",
  discord: "discord.production: verified Discord signatures required; verified sources allowed by default.",
  slack: "slack.production: verified Slack signatures required; verified sources allowed by default.",
  generic: "Generic endpoint policy: shared-secret verification follows the endpoint configuration; unverified input is denied when security is configured.",
};

function templateSetupSteps(template: ExternalInputTemplate | null): string[] {
  const rawSteps = template?.setup_steps;
  if (!Array.isArray(rawSteps)) return [];
  return rawSteps
    .map((step) => String(step ?? "").trim())
    .filter(Boolean);
}

/** Return provider-specific setup guidance for the selected input template. */
export function externalInputSetupGuide(
  template: ExternalInputTemplate | null,
  provider: string,
): string {
  const normalizedProvider = provider.trim().toLowerCase();
  const steps = templateSetupSteps(template);
  const resolvedSteps = steps.length
    ? steps
    : (FALLBACK_SETUP_STEPS[normalizedProvider] ?? [
      `Select the ${normalizedProvider || "external"} input template.`,
      "Review its route, credentials, endpoint, and source rules before enabling it.",
    ]);
  return resolvedSteps.map((step, index) => `${index + 1}. ${step}`).join("\n");
}

/** Return the built-in audience-policy summary for an input provider. */
export function externalInputPolicySummary(provider: string): string {
  const normalizedProvider = provider.trim().toLowerCase();
  return POLICY_SUMMARIES[normalizedProvider]
    ?? `${normalizedProvider || "Custom"} input policy: review the selected endpoint's verification and audience rules.`;
}
