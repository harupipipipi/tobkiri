import type { Root, RootContent, Text } from "mdast";

export type MessageMention = { label: string; syntax?: string; kind: string; id: string };

/** Highlight semantic mentions in prose without rewriting code, links, or HTML. */
export function remarkMessageMentions(mentions: MessageMention[] = []) {
  const syntaxes = [...new Set(mentions.flatMap((mention) => [mention.syntax, `@${mention.label}`])
    .filter((syntax): syntax is string => Boolean(syntax?.startsWith("@") && syntax.length > 1)))];
  if (syntaxes.length === 0) return;
  const alternatives = syntaxes.sort((a, b) => b.length - a.length)
    .map((syntax) => syntax.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(?<![\\p{L}\\p{N}_@])(?:${alternatives.join("|")})(?![\\p{L}\\p{N}_])`, "giu");

  const splitText = (node: Text): RootContent[] => {
    const parts: RootContent[] = [];
    let start = 0;
    for (const match of node.value.matchAll(pattern)) {
      const index = match.index;
      if (index > start) parts.push({ type: "text", value: node.value.slice(start, index) });
      parts.push({
        type: "strong",
        data: { hName: "span", hProperties: { className: ["rumi-message-mention"] } },
        children: [{ type: "text", value: match[0] }],
      });
      start = index + match[0].length;
    }
    if (start === 0) return [node];
    if (start < node.value.length) parts.push({ type: "text", value: node.value.slice(start) });
    return parts;
  };

  const visit = (node: Root | RootContent) => {
    if (["code", "inlineCode", "link", "linkReference", "html"].includes(node.type)) return;
    if (!("children" in node)) return;
    // Only text children are replaced; all replacements remain phrasing content.
    const children = node.children as RootContent[];
    for (let index = children.length - 1; index >= 0; index--) {
      const child = children[index];
      if (child.type === "text") children.splice(index, 1, ...splitText(child));
      else visit(child);
    }
  };
  return (tree: Root) => visit(tree);
}
