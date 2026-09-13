export const CHAT_FOLLOW_BOTTOM_THRESHOLD_PX = 96;

export type ScrollMetrics = {
  clientHeight: number;
  scrollHeight: number;
  scrollTop: number;
};

export function isMessageScrollerNearBottom(
  metrics: ScrollMetrics,
  threshold = CHAT_FOLLOW_BOTTOM_THRESHOLD_PX,
): boolean {
  const distanceFromBottom = metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight;
  return distanceFromBottom <= Math.max(0, threshold);
}
