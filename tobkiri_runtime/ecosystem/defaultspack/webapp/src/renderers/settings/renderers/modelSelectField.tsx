import { useEffect, useRef, useState } from "react";

import type { ModelSearchItem } from "../../../lib/api";
import {
  ModelSearchPicker,
  modelProviderOptions,
  modelFieldOptionToModelSelectOption,
  parseModelProviderQuery,
  parseModelSelectorSchema,
  type ModelSelectorSchema,
  type ModelSelectOption,
} from "../../../features/models";
import { settingsApiResources } from "../../../features/settings/resources/settingsApiResources";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { fieldOptions, modelSelectTargetFieldId, SettingsFieldShell } from "./settingsFieldRendererUtils";

export function SettingsModelSearchField({
  value,
  options,
  onChange,
  placeholder = "model/provider/特徴メモで検索",
  selectorSchema,
}: {
  value: string;
  options: ModelSelectOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  selectorSchema?: ModelSelectorSchema;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [remoteResults, setRemoteResults] = useState<ModelSearchItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const searchRequestSeq = useRef(0);
  const trimmedQuery = query.trim();
  const resolvedSelectorSchema = selectorSchema ?? parseModelSelectorSchema(undefined);
  const providerQueryState = parseModelProviderQuery(
    query,
    modelProviderOptions(options),
    resolvedSelectorSchema.layout.provider_trigger,
  );

  useEffect(() => {
    if (!open) return;
    searchRequestSeq.current += 1;
    const requestSeq = searchRequestSeq.current;
    if (!trimmedQuery || providerQueryState.active) {
      setRemoteResults([]);
      setBusy(false);
      setError("");
      return;
    }
    let disposed = false;
    const timer = window.setTimeout(() => {
      setBusy(true);
      setError("");
      settingsApiResources.searchModels({
        query: providerQueryState.providerId ? providerQueryState.modelQuery : trimmedQuery,
        max_results: 30,
        ...(providerQueryState.providerId ? { provider_id: providerQueryState.providerId } : {}),
      })
        .then((result) => {
          if (disposed || requestSeq !== searchRequestSeq.current) return;
          setRemoteResults(result.models ?? []);
        })
        .catch((searchError: unknown) => {
          if (disposed || requestSeq !== searchRequestSeq.current) return;
          setRemoteResults([]);
          setError(searchError instanceof Error ? searchError.message : "モデル検索に失敗しました");
        })
        .finally(() => {
          if (!disposed && requestSeq === searchRequestSeq.current) setBusy(false);
        });
    }, 160);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [
    open,
    providerQueryState.active,
    providerQueryState.modelQuery,
    providerQueryState.providerId,
    trimmedQuery,
  ]);

  return (
    <div data-settings-renderer="model_select">
      <ModelSearchPicker
        value={value}
        options={options}
        remoteResults={remoteResults}
        query={query}
        loading={busy}
        error={error}
        placeholder={placeholder}
        selectorSchema={selectorSchema}
        surface="settings"
        open={open}
        onOpenChange={setOpen}
        onChange={onChange}
        onQueryChange={setQuery}
      />
    </div>
  );
}

export function BuiltinModelSelectRenderer({ sectionId, field, value, sectionValues, onChange }: SettingsFieldRendererProps) {
  const targetFieldId = modelSelectTargetFieldId(field);
  const selectedValue = String(sectionValues?.[targetFieldId] ?? value ?? field.default ?? "");
  return (
    <SettingsFieldShell field={field}>
      <SettingsModelSearchField
        value={selectedValue}
        options={fieldOptions(field).map(modelFieldOptionToModelSelectOption)}
        selectorSchema={parseModelSelectorSchema(field.selector_schema)}
        onChange={(nextValue) => onChange(sectionId, targetFieldId, nextValue)}
      />
    </SettingsFieldShell>
  );
}
