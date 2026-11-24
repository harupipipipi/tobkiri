/**
 * Bounded Stop latch for the Core-host page. At most one backend
 * `core_stop_session` request is issued per accepted display generation:
 * the latch, pending label and disabled button are all applied
 * synchronously before frame teardown so reentrant Stop events are
 * ignored. Settled results publish fixed labels only while their accepted
 * generation is still current; raw backend errors are consumed.
 */

const STOP_PENDING = 'Stopping Profile…';
const STOP_RESOLVED = 'Profile stopped.';
const STOP_REJECTED = 'Profile stop failed.';

/** Host-page hooks supplied by `main.ts`. */
export interface ProfileStopSession {
  /** Current local UI display generation. */
  getGeneration(): number;
  /** Advance the local UI generation once (`++generation`); returns it. */
  advanceGeneration(): number;
  /** Synchronous frame/session teardown; may reenter {@link stop}. */
  prepareStop(): void;
  /** Invoke the existing zero-argument backend stop command. */
  invokeStop(): Promise<unknown>;
}

export class ProfileStopController {
  private acceptedGeneration = -1;

  constructor(
    private readonly statusElement: HTMLElement,
    private readonly stopButton: HTMLButtonElement,
    private readonly session: ProfileStopSession,
  ) {}

  /**
   * Accept at most one Stop per display generation. Shared by the Stop
   * button's click listener and the once:true pagehide listener.
   */
  readonly stop = (): void => {
    if (this.acceptedGeneration === this.session.getGeneration()) return;
    const generation = this.session.advanceGeneration();
    this.acceptedGeneration = generation;
    this.statusElement.textContent = STOP_PENDING;
    this.stopButton.disabled = true;
    this.session.prepareStop();
    let outcome: Promise<unknown>;
    try {
      outcome = this.session.invokeStop();
    } catch {
      this.publish(generation, false);
      return;
    }
    void Promise.resolve(outcome).then(
      () => this.publish(generation, true),
      () => this.publish(generation, false),
    );
  };

  /**
   * Rearm the latch and button for a new display generation. Must only run
   * from the explicit Profile Start submit, after its generation advance.
   */
  resetForStart(): void {
    this.acceptedGeneration = -1;
    this.stopButton.disabled = false;
  }

  private publish(generation: number, succeeded: boolean): void {
    if (generation !== this.session.getGeneration()) return;
    this.statusElement.textContent = succeeded ? STOP_RESOLVED : STOP_REJECTED;
  }
}
