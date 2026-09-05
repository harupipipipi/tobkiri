import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AlertTriangle, Monitor, X } from "lucide-react";

import { cn } from "../../lib/cn";
import { ErrorNotice } from "../ErrorNotice";
import {
  providerIsDesktopReady,
  providerLabel,
} from "../../features/sandboxes/runtimeStatus";
import type {
  CreateDesktopRequest,
  DesktopResolution,
  DesktopStarter,
  RuntimeProviderStatus,
  SandboxTemplate,
} from "../../features/sandboxes/types";

type DesktopCreateDialogProps = {
  isOpen: boolean;
  templates: SandboxTemplate[];
  providers: RuntimeProviderStatus[];
  selectedProviderId?: string | null;
  loading?: boolean;
  error?: string | null;
  onClose: () => void;
  onCreate: (request: CreateDesktopRequest) => Promise<void> | void;
};

const RESOLUTIONS: DesktopResolution[] = [
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
];
const GUEST_PROVISIONING_CAPABILITIES = ["sandbox.exec", "sandbox.files"];

function splitList(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function resolutionLabel(resolution: DesktopResolution): string {
  return `${resolution.width} x ${resolution.height}`;
}

function templateLabel(template: SandboxTemplate): string {
  return template.name || template.template_id;
}

function templateMatchesProvider(
  template: SandboxTemplate,
  provider: RuntimeProviderStatus | null,
): boolean {
  if (!provider || provider.provider_id === "auto") return true;
  const requirements = template.provider_requirements ?? [];
  if (requirements.length === 0) return true;
  const capabilities = new Set(provider.capabilities ?? []);
  return requirements.every((requirement) => capabilities.has(requirement));
}

function templateSupportsGuestProvisioning(
  template: SandboxTemplate | null,
): boolean {
  if (!template) return false;
  const capabilities = new Set([
    ...(template.provider_requirements ?? []),
    ...(template.capabilities ?? []),
  ]);
  return GUEST_PROVISIONING_CAPABILITIES.every((capability) =>
    capabilities.has(capability),
  );
}

type DesktopAccessMode = "owner_only" | "key_required" | "request_required" | "shared_link";
type DesktopStarterSelection = "template_default" | DesktopStarter;

function starterLabel(starter: DesktopStarter | undefined): string {
  if (starter === "browser_url") return "Browser URL";
  if (starter === "browser") return "Browser";
  if (starter === "terminal") return "Terminal";
  return "Empty";
}

export function DesktopCreateDialog({
  isOpen,
  templates,
  providers,
  selectedProviderId,
  loading = false,
  error,
  onClose,
  onCreate,
}: DesktopCreateDialogProps) {
  const [name, setName] = useState("Ubuntu Desktop");
  const [templateId, setTemplateId] = useState("");
  const [providerId, setProviderId] = useState("auto");
  const [resolution, setResolution] = useState<DesktopResolution>(
    RESOLUTIONS[0],
  );
  const [starter, setStarter] = useState<DesktopStarterSelection>("template_default");
  const [browserUrl, setBrowserUrl] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [workspaceAccess, setWorkspaceAccess] = useState<
    "none" | "read_only" | "overlay"
  >("none");
  const [assignedAgent, setAssignedAgent] = useState("");
  const [role, setRole] = useState("");
  const [ruleText, setRuleText] = useState("");
  const [accessMode, setAccessMode] = useState<DesktopAccessMode>("owner_only");
  const [accessKey, setAccessKey] = useState("");
  const [provisioningApps, setProvisioningApps] = useState("");
  const [provisioningMcp, setProvisioningMcp] = useState("");

  const selectedProvider = useMemo(() => {
    if (providerId !== "auto")
      return (
        providers.find((provider) => provider.provider_id === providerId) ??
        null
      );
    return (
      providers.find(
        (provider) =>
          provider.provider_id === selectedProviderId &&
          providerIsDesktopReady(provider),
      ) ??
      providers.find(
        (provider) => provider.selected && providerIsDesktopReady(provider),
      ) ??
      providers.find(providerIsDesktopReady) ??
      providers.find(
        (provider) => provider.provider_id === selectedProviderId,
      ) ??
      providers.find((provider) => provider.selected) ??
      null
    );
  }, [providerId, providers, selectedProviderId]);
  const selectedProviderReady = selectedProvider
    ? providerIsDesktopReady(selectedProvider)
    : providers.length === 0;
  const visibleTemplates = useMemo(() => {
    if (!selectedProvider) return templates;
    return templates.filter((template) =>
      templateMatchesProvider(template, selectedProvider),
    );
  }, [selectedProvider, templates]);
  const firstTemplate = visibleTemplates[0]?.template_id ?? "";
  const effectiveTemplateId = templateId || firstTemplate;
  const selectedTemplate =
    visibleTemplates.find(
      (template) => template.template_id === effectiveTemplateId,
    ) ??
    visibleTemplates[0] ??
    null;
  const selectedTemplateCompatible = selectedTemplate
    ? selectedProviderReady &&
      templateMatchesProvider(selectedTemplate, selectedProvider)
    : false;
  const selectedTemplateSupportsProvisioning =
    templateSupportsGuestProvisioning(selectedTemplate);
  const templateStarter = selectedTemplate?.desktop?.starter ?? "empty";
  const effectiveStarter = starter === "template_default" ? templateStarter : starter;
  const showLinuxNativeWarning =
    selectedProvider?.provider_id === "linux_native" ||
    selectedProvider?.isolation?.host_process_namespace ||
    selectedProvider?.isolation?.host_filesystem_shared ||
    selectedProvider?.isolation?.host_network_shared ||
    selectedProvider?.isolation?.sandbox_workspace_shared ||
    selectedProvider?.isolation?.sandbox_process_namespace_shared ||
    selectedProvider?.isolation?.sandbox_network_namespace_shared;

  useEffect(() => {
    if (!workspaceId.trim()) {
      setWorkspaceAccess("none");
      return;
    }
    setWorkspaceAccess((current) =>
      current === "none" ? "read_only" : current,
    );
  }, [workspaceId]);

  useEffect(() => {
    if (!isOpen) return;
    if (!firstTemplate) {
      if (templateId) setTemplateId("");
      return;
    }
    if (
      !visibleTemplates.some(
        (template) => template.template_id === effectiveTemplateId,
      )
    ) {
      setTemplateId(firstTemplate);
    }
  }, [
    effectiveTemplateId,
    firstTemplate,
    isOpen,
    templateId,
    visibleTemplates,
  ]);

  useEffect(() => {
    if (selectedTemplateSupportsProvisioning) return;
    if (provisioningApps) setProvisioningApps("");
    if (provisioningMcp) setProvisioningMcp("");
  }, [provisioningApps, provisioningMcp, selectedTemplateSupportsProvisioning]);

  if (!isOpen) return null;

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!effectiveTemplateId || !selectedTemplateCompatible) return;
    void onCreate({
      name: name.trim() || "Desktop",
      template_id: effectiveTemplateId,
      provider_id: providerId === "auto" ? null : providerId,
      resolution,
      ...(starter === "template_default" ? {} : { starter }),
      browser_url:
        effectiveStarter === "browser_url" ? browserUrl.trim() || undefined : undefined,
      workspace_id: workspaceId.trim() || null,
      workspace_access: workspaceAccess,
      assigned_agent: assignedAgent.trim() || null,
      role: role.trim() || null,
      rules: ruleText.trim()
        ? { role: role.trim() || null, rule_ids: splitList(ruleText) }
        : null,
      access: {
        mode: accessMode,
        ...(accessMode === "key_required" && accessKey
          ? { access_key: accessKey }
          : {}),
      },
      provisioning:
        selectedTemplateSupportsProvisioning &&
        (provisioningApps.trim() || provisioningMcp.trim())
          ? {
              apps: splitList(provisioningApps),
              mcp_servers: splitList(provisioningMcp),
            }
          : null,
    });
  };

  return (
    <div className="absolute inset-0 rumi-layer-modal flex items-center justify-center bg-black/55 p-4">
      <form
        onSubmit={handleSubmit}
        className="flex max-h-[calc(100vh-48px)] w-[min(720px,100%)] flex-col overflow-hidden rounded-lg border border-zinc-800 bg-[#0a0a0c] shadow-2xl"
      >
        <div className="flex items-center justify-between gap-3 border-b border-zinc-800/70 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-700 bg-zinc-900 text-zinc-100">
              <Monitor size={15} />
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-zinc-100">
                New Desktop
              </p>
              <p className="truncate text-xs text-zinc-500">
                {selectedTemplate?.network_policy?.summary ||
                  "Network policy is resolved by the backend template."}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
            aria-label="Close new desktop dialog"
          >
            <X size={15} />
          </button>
        </div>

        <div className="min-h-0 overflow-y-auto px-4 py-4">
          {templates.length === 0 ? (
            <div className="rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-sm text-amber-100">
              Desktop templates are unavailable from the backend.
            </div>
          ) : (
            <>
              {visibleTemplates.length === 0 && (
                <div className="mb-4 rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-sm text-amber-100">
                  No desktop templates match the selected runtime provider.
                </div>
              )}
              <div className="grid gap-4 md:grid-cols-2">
                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Name</span>
                  <input
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  />
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Template</span>
                  <select
                    value={effectiveTemplateId}
                    onChange={(event) => setTemplateId(event.target.value)}
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  >
                    {visibleTemplates.map((template) => (
                      <option
                        key={template.template_id}
                        value={template.template_id}
                      >
                        {templateLabel(template)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Provider</span>
                  <select
                    value={providerId}
                    onChange={(event) => setProviderId(event.target.value)}
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  >
                    <option value="auto">Auto</option>
                    {providers.map((provider) => (
                      <option
                        key={provider.provider_id}
                        value={provider.provider_id}
                        disabled={!providerIsDesktopReady(provider)}
                      >
                        {providerLabel(provider)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Resolution</span>
                  <select
                    value={resolutionLabel(resolution)}
                    onChange={(event) => {
                      const next = RESOLUTIONS.find(
                        (candidate) =>
                          resolutionLabel(candidate) === event.target.value,
                      );
                      if (next) setResolution(next);
                    }}
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  >
                    {RESOLUTIONS.map((candidate) => (
                      <option
                        key={resolutionLabel(candidate)}
                        value={resolutionLabel(candidate)}
                      >
                        {resolutionLabel(candidate)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Starter</span>
                  <select
                    value={starter}
                    onChange={(event) =>
                      setStarter(event.target.value as DesktopStarterSelection)
                    }
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  >
                    <option value="template_default">
                      Template default ({starterLabel(templateStarter)})
                    </option>
                    <option value="empty">Empty</option>
                    <option value="browser">Browser</option>
                    <option value="browser_url">Browser URL</option>
                    <option value="terminal">Terminal</option>
                  </select>
                </label>

                {effectiveStarter === "browser_url" && (
                  <label className="grid gap-1.5 text-xs text-zinc-400">
                    <span>URL</span>
                    <input
                      value={browserUrl}
                      onChange={(event) => setBrowserUrl(event.target.value)}
                      placeholder="https://example.com"
                      className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                    />
                  </label>
                )}

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Workspace binding</span>
                  <input
                    value={workspaceId}
                    onChange={(event) => setWorkspaceId(event.target.value)}
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  />
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Workspace access</span>
                  <select
                    value={workspaceAccess}
                    onChange={(event) =>
                      setWorkspaceAccess(
                        event.target.value as typeof workspaceAccess,
                      )
                    }
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  >
                    <option value="none">None</option>
                    <option value="read_only">Read only</option>
                    <option value="overlay">Writable overlay</option>
                  </select>
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400 md:col-span-2">
                  <span>Agent assignment</span>
                  <input
                    value={assignedAgent}
                    onChange={(event) => setAssignedAgent(event.target.value)}
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  />
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Role</span>
                  <input
                    value={role}
                    onChange={(event) => setRole(event.target.value)}
                    placeholder="coding assistant"
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  />
                </label>

                <label className="grid gap-1.5 text-xs text-zinc-400">
                  <span>Access</span>
                  <select
                    value={accessMode}
                    onChange={(event) =>
                      setAccessMode(event.target.value as typeof accessMode)
                    }
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  >
                    <option value="owner_only">Owner only</option>
                    <option value="shared_link">Shared link</option>
                    <option value="key_required">Key required</option>
                    <option value="request_required">Request required</option>
                  </select>
                </label>

                {accessMode === "key_required" && (
                  <label className="grid gap-1.5 text-xs text-zinc-400">
                    <span>Access key</span>
                    <input
                      value={accessKey}
                      onChange={(event) => setAccessKey(event.target.value)}
                      type="password"
                      className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                    />
                  </label>
                )}

                <label className="grid gap-1.5 text-xs text-zinc-400 md:col-span-2">
                  <span>Rules</span>
                  <input
                    value={ruleText}
                    onChange={(event) => setRuleText(event.target.value)}
                    placeholder="browser-only, keep workspace clean"
                    className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                  />
                </label>

                {selectedTemplateSupportsProvisioning && (
                  <>
                    <label className="grid gap-1.5 text-xs text-zinc-400">
                      <span>Apps</span>
                      <input
                        value={provisioningApps}
                        onChange={(event) =>
                          setProvisioningApps(event.target.value)
                        }
                        placeholder={
                          selectedTemplate?.provisioning?.apps?.join(", ") ||
                          "Template default"
                        }
                        className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                      />
                    </label>

                    <label className="grid gap-1.5 text-xs text-zinc-400">
                      <span>MCP servers</span>
                      <input
                        value={provisioningMcp}
                        onChange={(event) =>
                          setProvisioningMcp(event.target.value)
                        }
                        placeholder={
                          selectedTemplate?.provisioning?.mcp_servers?.join(
                            ", ",
                          ) || "playwright"
                        }
                        className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-100 outline-none focus:border-zinc-600"
                      />
                    </label>
                  </>
                )}
              </div>
            </>
          )}

          {showLinuxNativeWarning && (
            <div className="mt-4 flex gap-2 rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-xs text-amber-100">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" />
              <p>
                Linux native desktops can share host process, filesystem, or
                network namespaces beyond configured sandboxing. The backend
                isolation facts determine the exact boundary.
              </p>
            </div>
          )}

          {error && (
            <ErrorNotice
              className="mt-4 text-xs"
              copyLabel="デスクトップ作成エラーをコピー"
              message={error}
            />
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-zinc-800/70 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="h-8 rounded-md border border-zinc-800 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={
              loading ||
              visibleTemplates.length === 0 ||
              !selectedTemplateCompatible
            }
            className="h-8 rounded-md bg-zinc-100 px-3 text-xs font-semibold text-zinc-950 hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Creating..." : "Create Desktop"}
          </button>
        </div>
      </form>
    </div>
  );
}
