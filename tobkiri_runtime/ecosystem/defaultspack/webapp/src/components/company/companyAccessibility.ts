export type CompositeNavigationKey =
  | "ArrowDown"
  | "ArrowLeft"
  | "ArrowRight"
  | "ArrowUp"
  | "End"
  | "Home";

export function nextCompositeIndex(
  currentIndex: number,
  itemCount: number,
  key: string,
): number | null {
  if (itemCount <= 0) return null;
  const current = Math.min(Math.max(currentIndex, 0), itemCount - 1);
  if (key === "Home") return 0;
  if (key === "End") return itemCount - 1;
  if (key === "ArrowRight" || key === "ArrowDown") {
    return (current + 1) % itemCount;
  }
  if (key === "ArrowLeft" || key === "ArrowUp") {
    return (current - 1 + itemCount) % itemCount;
  }
  return null;
}
