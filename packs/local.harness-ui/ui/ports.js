/** Port client for an isolated Pack iframe. The host binds the Core UI session. */
export { toCanonFloat } from "./float.js";

/** An opaque Core-owned stream. Pulls retain the original Core call context. */
class PackStream {
  #client;
  #handle;
  #closed = false;
  constructor(client, handle) { this.#client = client; this.#handle = handle; }
  async pull(max = 32, options = {}) {
    if (!Number.isSafeInteger(max) || max < 1 || max > 32) throw new RangeError("stream pull max must be 1..32");
    if (this.#closed) throw new Error("Pack stream is closed");
    try {
      const values = await this.#client.request({ type: "stream-pull", handle: this.#handle, max }, options);
      if (!Array.isArray(values) || values.length > max) throw new Error("invalid bounded stream response");
      if (!values.length) { this.#closed = true; this.#client.forget(this); }
      return values;
    } catch (error) {
      void this.cancel().catch(() => {});
      throw error;
    }
  }
  async cancel() {
    if (this.#closed) return;
    this.#closed = true;
    this.#client.forget(this);
    await this.#client.request({ type: "stream-cancel", handle: this.#handle });
  }
  closeLocally() { this.#closed = true; }
}

export class PackPortClient {
  #port;
  #next = 1;
  #pending = new Map();
  #streams = new Set();
  #closed = false;

  constructor(port) {
    this.#port = port;
    port.onmessage = ({ data }) => {
      const pending = this.#pending.get(data?.id);
      if (!pending) return;
      this.#pending.delete(data.id);
      pending.cleanup();
      if (data.error) pending.reject(new Error(data.error.message ?? "Port call failed"));
      else pending.resolve(data.result);
    };
    port.start();
  }

  /** Send only a shape, operation and value. No principal or approved flag. */
  async call(shape, op, args, options = {}) {
    if (typeof shape !== "string" || typeof op !== "string") throw new TypeError("shape and op must be strings");
    const result = await this.request({ type: "call", shape, op, args }, options);
    if (result?.kind === "value") return result.value;
    if (result?.kind === "stream" && Number.isSafeInteger(result.handle) && result.handle > 0) {
      const stream = new PackStream(this, result.handle);
      this.#streams.add(stream);
      return stream;
    }
    throw new Error("invalid Core UI call result");
  }

  /** Internal transport; the Core checks handle ownership and every call's scope. */
  request(message, { timeoutMs = 30000, signal } = {}) {
    if (this.#closed) return Promise.reject(new Error("Pack UI session closed"));
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 300000) return Promise.reject(new RangeError("timeout must be 1..300000 ms"));
    if (signal?.aborted) return Promise.reject(new Error("Port call cancelled"));
    if (!Number.isSafeInteger(this.#next)) return Promise.reject(new Error("Pack UI request counter exhausted"));
    const id = this.#next++;
    return new Promise((resolve, reject) => {
      let timer;
      const cancel = (message) => {
        if (!this.#pending.delete(id)) return;
        cleanup();
        this.#port.postMessage({ type: "cancel", id });
        reject(new Error(message));
      };
      const aborted = () => cancel("Port call cancelled");
      const cleanup = () => { clearTimeout(timer); signal?.removeEventListener("abort", aborted); };
      timer = setTimeout(() => cancel("Port call timed out"), timeoutMs);
      this.#pending.set(id, { resolve, reject, cleanup });
      signal?.addEventListener("abort", aborted, { once: true });
      try { this.#port.postMessage({ ...message, id }); }
      catch (error) { this.#pending.delete(id); cleanup(); reject(error); }
    });
  }
  forget(stream) { this.#streams.delete(stream); }

  close() {
    if (this.#closed) return;
    this.#closed = true;
    // The host detaches the bound UI session and Core cancels its scoped streams.
    try { this.#port.postMessage({ type: "close" }); } catch {}
    for (const stream of this.#streams) stream.closeLocally();
    this.#streams.clear();
    for (const pending of this.#pending.values()) {
      pending.cleanup();
      pending.reject(new Error("Pack UI session closed"));
    }
    this.#pending.clear();
    this.#port.close();
  }
}

/** Bind a one-use channel supplied by the embedding host; no global IPC API. */
export const connectPackUi = (expectedParent = window.parent) => new Promise((resolve) => {
  const connect = (event) => {
    if (event.source !== expectedParent || event.data?.type !== "tobkiri-pack-channel" || event.ports.length !== 1) return;
    window.removeEventListener("message", connect);
    const client = new PackPortClient(event.ports[0]);
    window.addEventListener("pagehide", () => client.close(), { once: true });
    resolve(client);
  };
  window.addEventListener("message", connect);
});
