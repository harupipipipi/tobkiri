/** Hidden owner records remain addressable but do not appear in the main history. */
export function conversationVisibleInHistory(conversation: { metadata?: Record<string, unknown> | null }): boolean {
  const metadata = conversation.metadata;
  return !metadata || !Object.prototype.hasOwnProperty.call(metadata, "is_hidden") || metadata.is_hidden !== true;
}
