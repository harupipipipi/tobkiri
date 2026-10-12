import { useId, useState } from "react";
import { Info } from "lucide-react";

import {
  enrichModelSelectOptions,
  mergeRegisteredModelProfileOptions,
  ModelSearchPicker,
  findSelectedModelOption,
  modelFieldOptionToModelSelectOption,
  modelOptionNeedsVisionRecommendation,
  parseModelSelectorSchema,
  type ModelSelectorSchema,
  type ModelSelectOption,
} from "../../../features/models";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { fieldOptions, modelSelectTargetFieldId, SettingsFieldShell } from "./settingsFieldRendererUtils";

const REGISTERED_ROUTE_MODEL_TARGETS = new Set([
  "preferred_model",
  "main_model",
  "lightweight_model",
]);

export function modelSelectOptionsForSettingsTarget(
  targetFieldId: string,
  catalogOptions: ModelSelectOption[],
  modelProfiles: SettingsFieldRendererProps["modelProfiles"] = [],
): ModelSelectOption[] {
  if (REGISTERED_ROUTE_MODEL_TARGETS.has(targetFieldId)) {
    return mergeRegisteredModelProfileOptions(catalogOptions, modelProfiles);
  }
  return enrichModelSelectOptions(catalogOptions, modelProfiles);
}

export function SettingsModelSearchField({
  value,
  options,
  onChange,
  recommendVision = false,
  placeholder = "model/provider/特徴メモで検索",
  selectorSchema,
}: {
  value: string;
  options: ModelSelectOption[];
  onChange: (value: string) => void;
  recommendVision?: boolean;
  placeholder?: string;
  selectorSchema?: ModelSelectorSchema;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [rememberedSelectedOption, setRememberedSelectedOption] = useState<ModelSelectOption | null>(null);
  const rememberedOptions = rememberedSelectedOption?.value === value
    && !options.some((option) => (
      option.value === value || option.qualified_model_id === value
    ))
    ? [rememberedSelectedOption]
    : [];
  const pickerOptions = rememberedOptions.length > 0
    ? [...options, ...rememberedOptions]
    : options;
  const selectedOption = findSelectedModelOption(pickerOptions, value);
  const needsVisionRecommendation = recommendVision
    && modelOptionNeedsVisionRecommendation(selectedOption, value);

  return (
    <div data-settings-renderer="model_select">
      <ModelSearchPicker
        value={value}
        options={pickerOptions}
        query={query}
        placeholder={placeholder}
        selectorSchema={selectorSchema}
        surface="settings"
        open={open}
        onOpenChange={setOpen}
        onChange={onChange}
        onSelectedOptionChange={setRememberedSelectedOption}
        onQueryChange={setQuery}
      />
      {needsVisionRecommendation && <VisionRecommendation />}
    </div>
  );
}

export function BuiltinModelSelectRenderer({
  sectionId,
  field,
  value,
  sectionValues,
  modelProfiles = [],
  onChange,
}: SettingsFieldRendererProps) {
  const targetFieldId = modelSelectTargetFieldId(field);
  const selectedValue = String(sectionValues?.[targetFieldId] ?? value ?? field.default ?? "");
  const options = modelSelectOptionsForSettingsTarget(
    targetFieldId,
    fieldOptions(field).map(modelFieldOptionToModelSelectOption),
    modelProfiles,
  );
  const isLightweightModel = targetFieldId === "lightweight_model";
  return (
    <SettingsFieldShell field={field}>
      <SettingsModelSearchField
        value={selectedValue}
        options={options}
        selectorSchema={parseModelSelectorSchema(field.selector_schema)}
        recommendVision={isLightweightModel}
        onChange={(nextValue) => onChange(sectionId, targetFieldId, nextValue)}
      />
    </SettingsFieldShell>
  );
}

function VisionRecommendation() {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();
  return (
    <span
      className="relative mt-2 inline-flex items-center gap-1.5 text-[11px] text-red-300"
      data-settings-vision-recommendation
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <span>Vision対応モデルを推奨</span>
      <button
        type="button"
        aria-label="Vision対応モデルを推奨する理由"
        aria-describedby={open ? tooltipId : undefined}
        aria-expanded={open}
        onClick={() => setOpen(true)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className="rounded text-red-300/80 outline-none hover:text-red-200 focus-visible:ring-1 focus-visible:ring-red-300/70"
      >
        <Info size={13} aria-hidden="true" />
      </button>
      {open && (
        <span
          id={tooltipId}
          role="tooltip"
          className="absolute bottom-full left-0 rumi-layer-local-popover mb-1 w-60 rounded-md border border-red-400/25 bg-zinc-950 px-2 py-1.5 leading-4 text-zinc-300 shadow-xl"
        >
          画像の添付や引き継ぎを使う場合は、Vision対応の軽量モデルを選ぶと処理が安定します。
        </span>
      )}
    </span>
  );
}
