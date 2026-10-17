import type { ComposerSkillItem, DroppedWidget } from "../renderers/types";
import type { ComposerEntityReference } from "./composerReferences";
import { composerMentionMetadataFromWidgets } from "./composerWidgets";
import { confirmedComposerMentionRange } from "./composerMentionAnchors";

/** Return available skills selected by confirmed references or explicit widgets. */
export function confirmedComposerSkillIds(
  input: string,
  widgets: DroppedWidget[],
  references: ComposerEntityReference[],
  skills: ComposerSkillItem[],
): string[] {
  const available = new Set(skills.map((skill) => skill.id));
  const ids = new Set<string>();
  // References alone cannot revive a removed occurrence; picker and paste supply widgets.
  void references;
  for (const widget of widgets) {
    if (widget.enabled === false) continue;
    if (widget.metadata?.source === "composer_at_mention") {
      for (const mention of composerMentionMetadataFromWidgets([widget])) {
        if (mention.kind === "skill" && available.has(mention.id)
          && confirmedComposerMentionRange(widget, input)) ids.add(mention.id);
      }
    } else if (widget.type === "skill" || widget.widgetKind === "skill_prompt") {
      const id = widget.sourceItemId || widget.id;
      if (available.has(id)) ids.add(id);
    }
  }
  return [...ids];
}
