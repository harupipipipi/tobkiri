/** Orders all frontend model writes before the first saved request is captured. */
export class ConversationMutationBarrier {
  private writes = new Map<string, Promise<unknown>[]>();

  record(storeId: string, conversationId: string, write: Promise<unknown>): void {
    const key = `${storeId}\u0000${conversationId}`;
    this.writes.set(key, [...this.writes.get(key) ?? [], write]);
    void write.catch(() => undefined);
  }

  async wait(storeId: string, conversationId: string): Promise<void> {
    const key = `${storeId}\u0000${conversationId}`;
    let failure: PromiseRejectedResult | undefined;
    for (;;) {
      const batch = this.writes.get(key);
      if (!batch?.length) {
        if (failure) throw failure.reason;
        return;
      }
      const results = await Promise.allSettled(batch);
      failure ??= results.find((result): result is PromiseRejectedResult => result.status === "rejected");
      const consumed = new Set(batch);
      const remaining = (this.writes.get(key) ?? []).filter((write) => !consumed.has(write));
      if (remaining.length) this.writes.set(key, remaining);
      else this.writes.delete(key);
    }
  }
}
