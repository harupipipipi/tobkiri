import type { ComposerSkillItem, DroppedWidget } from "../renderers/types";
import type { ComposerEntityReference } from "./composerReferences";
import { composerMentionMetadataFromWidgets } from "./composerWidgets";
import { hasUnescapedMentionSyntax } from "./mentionContract";

/** Return available skills selected by confirmed references or explicit widgets. */
export function confirmedComposerSkillIds(
  input: string,
  widgets: DroppedWidget[],
  references: ComposerEntityReference[],
  skills: ComposerSkillItem[],
): string[] {
  const available = new Set(skills.map((skill) => skill.id));
  const ids = new Set<string>();
  const addReference = (reference: ComposerEntityReference) => {
    if (reference.kind === "skill" && available.has(reference.id)
      && hasUnescapedMentionSyntax(input, reference.syntax)) ids.add(reference.id);
  };
  for (const reference of references) addReference(reference);
  for (const widget of widgets) {
    if (widget.enabled === false) continue;
    if (widget.metadata?.source === "composer_at_mention") {
      for (const mention of composerMentionMetadataFromWidgets([widget])) {
        if (mention.kind === "skill") addReference({ ...mention, kind: "skill" });
      }
    } else if (widget.type === "skill" || widget.widgetKind === "skill_prompt") {
      const id = widget.sourceItemId || widget.id;
      if (available.has(id)) ids.add(id);
    }
  }
  return [...ids];
}
