/** Fixed limits for the trusted Launcher ↔ Core ↔ isolated-frame transport. */
export const HOST_MAX_BODY_BYTES = 1024 * 1024;
// Covers escaped strings plus fixed fields, safe-integer IDs, the 71-byte
// process namespace, and Core's `ui_` + 26-byte ULID receiver in the Pack
// response envelope. Host output uses the shorter fixed `core-host` receiver.
export const HOST_MAX_RESPONSE_TICKET_BYTES = HOST_MAX_BODY_BYTES * 6 + 256;
export const HOST_MAX_INFLIGHT = 4;
export const HOST_MAX_QUEUE_BYTES = HOST_MAX_INFLIGHT * HOST_MAX_RESPONSE_TICKET_BYTES;
export const UI_MAX_QUEUE_BYTES = HOST_MAX_INFLIGHT * (HOST_MAX_RESPONSE_TICKET_BYTES * 2);

export function utf8ByteLength(value: string, maximum = HOST_MAX_BODY_BYTES): number | null {
  let bytes = 0;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return null;
      bytes += 4;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) return null;
    else bytes += 3;
    if (bytes > maximum) return null;
  }
  return bytes;
}

/**
 * Measure JSON UTF-8 bytes before JSON.stringify allocates the serialized body.
 * Only StrictV1-compatible JSON values are admitted.
 */
export function jsonEncodedByteLength(value: unknown, maximum = HOST_MAX_BODY_BYTES): number | null {
  let bytes = 0;
  let nodes = 0;
  const active = new WeakSet<object>();
  const add = (count: number) => {
    bytes += count;
    return bytes <= maximum;
  };

  const stringBytes = (value: string): number | null => {
    let count = 2; // Quotes.
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index);
      if (code === 0x22 || code === 0x5c) count += 2;
      else if (code === 0x08 || code === 0x09 || code === 0x0a || code === 0x0c || code === 0x0d) count += 2;
      else if (code < 0x20) count += 6;
      else if (code < 0x80) count += 1;
      else if (code < 0x800) count += 2;
      else if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(index + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          count += 4;
          index += 1;
        } else count += 6; // JSON.stringify escapes an unpaired surrogate.
      } else if (code >= 0xdc00 && code <= 0xdfff) count += 6;
      else count += 3;
      if (count > maximum) return null;
    }
    return count;
  };

  const visit = (item: unknown, depth: number): boolean => {
    nodes += 1;
    if (nodes > maximum || depth > 64) return false;
    if (item === null) return add(4);
    if (item === true) return add(4);
    if (item === false) return add(5);
    if (typeof item === 'string') {
      const length = stringBytes(item);
      return length !== null && add(length);
    }
    if (typeof item === 'number') {
      return Number.isSafeInteger(item) && add(Object.is(item, -0) ? 1 : String(item).length);
    }
    if (typeof item !== 'object') return false;
    if (active.has(item)) return false;
    active.add(item);
    let okay = true;
    if (Array.isArray(item)) {
      if (item.length > maximum / 5) okay = false;
      else {
        okay = add(2);
        for (let index = 0; okay && index < item.length; index += 1) {
          if (index > 0) okay = add(1);
          if (!okay) break;
          okay = Object.hasOwn(item, index) ? visit(item[index], depth + 1) : add(4);
        }
      }
    } else {
      const prototype = Object.getPrototypeOf(item);
      if (prototype !== Object.prototype && prototype !== null) okay = false;
      else {
        okay = add(1);
        let first = true;
        for (const key in item as Record<string, unknown>) {
          if (!Object.hasOwn(item, key)) continue;
          if (!first) okay = add(1);
          first = false;
          const keyLength = stringBytes(key);
          if (keyLength === null || !okay) { okay = false; break; }
          okay = add(keyLength) && add(1);
          if (!okay) break;
          okay = visit((item as Record<string, unknown>)[key], depth + 1);
          if (!okay) break;
        }
        if (okay) okay = add(1);
      }
    }
    active.delete(item);
    return okay;
  };

  return visit(value, 0) ? bytes : null;
}

/** Six bytes per source byte covers JSON string escaping before IPC allocation. */
export function escapedEnvelopeByteBound(bodyBytes: number): number | null {
  if (!Number.isSafeInteger(bodyBytes) || bodyBytes < 0 || bodyBytes > HOST_MAX_BODY_BYTES) return null;
  const bound = bodyBytes * 6 + 256;
  return Number.isSafeInteger(bound) ? bound : null;
}

/** Tracks request plus worst-case response capacity for one frame generation. */
export class UiTransportQueue {
  #tickets = new Map<string, number>();
  #bytes = 0;

  reserve(messageId: number, requestBytes: number, receiver = 'default'): boolean {
    const key = `${receiver}\u0000${messageId}`;
    if (!Number.isSafeInteger(messageId) || messageId < 1 || this.#tickets.has(key)) return false;
    if (this.#tickets.size >= HOST_MAX_INFLIGHT) return false;
    const requestEnvelope = escapedEnvelopeByteBound(requestBytes);
    if (requestEnvelope === null) return false;
    const ticketBytes = requestEnvelope + HOST_MAX_RESPONSE_TICKET_BYTES;
    if (this.#bytes + ticketBytes > UI_MAX_QUEUE_BYTES) return false;
    this.#tickets.set(key, ticketBytes);
    this.#bytes += ticketBytes;
    return true;
  }

  acknowledge(messageId: number, receiver = 'default'): boolean {
    const key = `${receiver}\u0000${messageId}`;
    const bytes = this.#tickets.get(key);
    if (bytes === undefined) return false;
    this.#tickets.delete(key);
    this.#bytes -= bytes;
    return true;
  }

  clearReceiver(receiver: string): void {
    const prefix = `${receiver}\u0000`;
    for (const [key, bytes] of this.#tickets) {
      if (!key.startsWith(prefix)) continue;
      this.#tickets.delete(key);
      this.#bytes -= bytes;
    }
  }

  clear(): void {
    this.#tickets.clear();
    this.#bytes = 0;
  }

  get size(): number { return this.#tickets.size; }
  get reservedBytes(): number { return this.#bytes; }
}

/** Deduplicate trusted-host events within a process namespace. Ticket IDs
 * restart with a new TransportTickets instance, so an old watermark must not
 * suppress ticket 1 from the new namespace. */
export class HostOutputWatermark {
  #namespace = '';
  #lastTicketId = 0;

  useNamespace(namespace: string): void {
    if (namespace === this.#namespace) return;
    this.#namespace = namespace;
    this.#lastTicketId = 0;
  }

  accept(namespace: string, ticketId: number): boolean {
    if (namespace !== this.#namespace
      || !Number.isSafeInteger(ticketId) || ticketId < 1
      || ticketId <= this.#lastTicketId) return false;
    this.#lastTicketId = ticketId;
    return true;
  }
}

/** Keep event history bounded without first concatenating an unbounded chunk. */
export function appendBoundedHistory(current: string, chunk: string, maximumCodeUnits: number): string {
  if (!Number.isSafeInteger(maximumCodeUnits) || maximumCodeUnits < 1) return '';
  const boundedChunk = chunk.length > maximumCodeUnits ? chunk.slice(-maximumCodeUnits) : chunk;
  const available = maximumCodeUnits - boundedChunk.length;
  const prefix = available > 0 ? current.slice(-available) : '';
  return prefix + boundedChunk;
}
