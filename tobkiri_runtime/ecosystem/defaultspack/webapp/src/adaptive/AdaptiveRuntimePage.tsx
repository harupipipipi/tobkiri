import { Activity, Bot, FileSearch, GitBranch, Settings2, Sparkles } from "lucide-react";
import { useCallback, useState } from "react";

import { ActivityCenter } from "./ActivityCenter";
import { AutomationStudio } from "./AutomationStudio";
import { ContextBudgetPanel, EvidenceViewer, RepositoryMapPanel } from "./EvidencePanels";
import { OnboardingShell } from "./OnboardingShell";
import { OperatingProfilePage } from "./OperatingProfilePage";
import { adaptiveControlClass } from "./AdaptivePrimitives";
import { useAdaptiveTabs } from "./AdaptiveTabs";

type AdaptiveView = "onboarding" | "profile" | "activity" | "automation" | "context";

const views: Array<{ id: AdaptiveView; label: string; icon: typeof Sparkles }> = [
  { id: "onboarding", label: "Onboarding", icon: Sparkles },
  { id: "profile", label: "Profile", icon: Settings2 },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "automation", label: "Automation", icon: Bot },
  { id: "context", label: "Context", icon: FileSearch },
];
const viewIds = views.map((item) => item.id);

function AdaptiveContent({ view }: { view: AdaptiveView }) {
  if (view === "profile") return <OperatingProfilePage />;
  if (view === "activity") return <ActivityCenter />;
  if (view === "automation") return <AutomationStudio />;
  if (view === "context") {
    return (
      <div className="min-h-screen bg-zinc-950 p-3 text-zinc-100 md:p-5">
        <div className="mx-auto grid max-w-[1500px] gap-3 xl:grid-cols-[1fr_1fr]">
          <EvidenceViewer />
          <RepositoryMapPanel />
          <div className="xl:col-span-2">
            <ContextBudgetPanel />
          </div>
        </div>
      </div>
    );
  }
  return <OnboardingShell />;
}

export function AdaptiveRuntimePage() {
  const [view, setView] = useState<AdaptiveView>("onboarding");
  const selectView = useCallback((nextView: AdaptiveView) => setView(nextView), []);
  const tabs = useAdaptiveTabs({
    ids: viewIds,
    selectedId: view,
    onSelect: selectView,
    idPrefix: "adaptive-runtime-view",
  });
  return (
    <main className="min-h-screen bg-zinc-950 text-zinc-100" aria-label="Adaptive runtime">
      <div className="sticky top-0 rumi-layer-panel border-b border-zinc-800/80 bg-zinc-950/95 px-3 py-2 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-2">
            <GitBranch size={16} className="text-cyan-200" aria-hidden="true" />
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold text-zinc-50">Adaptive Runtime</h1>
              <p className="truncate text-xs text-zinc-500">Operating profiles, activity, automation, and evidence</p>
            </div>
          </div>
          <div
            className="flex gap-1 overflow-x-auto"
            role="tablist"
            aria-label="Adaptive runtime views"
            aria-orientation="horizontal"
          >
            {views.map((item) => {
              const Icon = item.icon;
              const active = item.id === view;
              return (
                <button
                  key={item.id}
                  type="button"
                  {...tabs.tabProps(item.id)}
                  className={`${adaptiveControlClass} ${active ? "border-cyan-400/40 bg-cyan-400/10 text-cyan-100" : ""}`}
                >
                  <Icon size={14} aria-hidden="true" />
                  {item.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>
      <div
        id={tabs.panelId(view)}
        role="tabpanel"
        aria-labelledby={tabs.tabId(view)}
        tabIndex={0}
      >
        <AdaptiveContent view={view} />
      </div>
    </main>
  );
}
