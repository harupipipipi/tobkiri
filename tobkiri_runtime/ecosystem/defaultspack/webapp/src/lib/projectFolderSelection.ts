/** Exclude duplicate operations and discard results from obsolete project forms. */
export class ProjectFolderSelection {
  private generation = 0;
  private pending: "selection" | "creation" | null = null;

  invalidate(): void {
    this.generation += 1;
    this.pending = null;
  }

  begin(operation: "selection" | "creation"): number | null {
    if (this.pending) return null;
    this.pending = operation;
    return ++this.generation;
  }

  matches(ticket: number): boolean {
    return ticket === this.generation && this.pending !== null;
  }

  finish(ticket: number): boolean {
    if (!this.matches(ticket)) return false;
    this.pending = null;
    return true;
  }

  get creating(): boolean {
    return this.pending === "creation";
  }
}
