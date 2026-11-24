/** Generic Pack renderer. A frame never receives its Core invocation token. */
import {
  HOST_MAX_BODY_BYTES,
  HOST_MAX_INFLIGHT,
  UiTransportQueue,
  jsonEncodedByteLength,
} from './boundedTransport';

export interface UiAttachment {
  uiSessionId: string;
  generation: number;
  packHash: string;
  entry: string;
  platform?: 'native' | 'windows';
}

export interface UiCall {
  id: number;
  shape: string;
  op: string;
  args: unknown;
}

export type UiRequest = UiCall
  | { id: number; type: 'stream-pull'; handle: number; max: number }
  | { id: number; type: 'stream-cancel'; handle: number };

export interface UiTransport {
  register(session: string, generation: number, call: UiRequest): Promise<void>;
  invoke(session: string, generation: number, call: UiRequest, requestJson: string, signal: AbortSignal): Promise<unknown>;
  cancel(session: string, generation: number, call: UiRequest): Promise<void>;
  acknowledge(session: string, generation: number, id: number): Promise<void>;
  close(session: string, generation: number): Promise<void>;
}

export interface IsolatedFrame {
  frame: HTMLIFrameElement;
  close(): void;
}

const MAX_PENDING = HOST_MAX_INFLIGHT;

function hasExactKeys(value: Record<string, unknown>, expected: string): boolean {
  const keys = expected.split(',');
  let count = 0;
  for (const key in value) {
    if (!Object.hasOwn(value, key)) continue;
    count += 1;
    if (!keys.includes(key)) return false;
  }
  return count === keys.length;
}

export function packUiUrl(attachment: UiAttachment): string {
  if (!/^sha256:[0-9a-f]{64}$/.test(attachment.packHash)
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$/.test(attachment.uiSessionId)
    || attachment.uiSessionId.includes('..')
    || !Number.isSafeInteger(attachment.generation) || attachment.generation < 1) throw new Error('Invalid UI attachment');
  const path = attachment.entry;
  if (!path || path.length > 4096 || /[\\:\x00-\x1f\x7f]/.test(path)
    || path.split('/').some((part) => !part || part === '.' || part === '..')) {
    throw new Error('Invalid UI entry');
  }
  if (attachment.platform !== undefined && attachment.platform !== 'native' && attachment.platform !== 'windows') throw new Error('Invalid UI platform');
  const encoded = path.split('/').map(encodeURIComponent).join('/');
  return attachment.platform === 'windows'
    ? `http://tobkiri-pack.localhost/${attachment.packHash.slice(7)}/${encoded}`
    : `tobkiri-pack://${attachment.packHash.slice(7)}/${encoded}`;
}

/** Validate structured clone data before forwarding it through trusted IPC. */
function message(data: unknown): UiRequest | { cancel: number } | { close: true } | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const value = data as Record<string, unknown>;
  if (value.type === 'close' && hasExactKeys(value, 'type')) return { close: true };
  if (!Number.isSafeInteger(value.id) || (value.id as number) < 1) return null;
  if (value.type === 'cancel' && hasExactKeys(value, 'id,type')) return { cancel: value.id as number };
  if ((value.type === 'stream-pull' && hasExactKeys(value, 'handle,id,max,type'))
    || (value.type === 'stream-cancel' && hasExactKeys(value, 'handle,id,type'))) {
    if (!Number.isSafeInteger(value.handle) || (value.handle as number) < 1) return null;
    if (value.type === 'stream-pull') {
      if (!Number.isSafeInteger(value.max) || (value.max as number) < 1 || (value.max as number) > 32) return null;
      return { type: 'stream-pull', id: value.id as number, handle: value.handle as number, max: value.max as number };
    }
    return { type: 'stream-cancel', id: value.id as number, handle: value.handle as number };
  }
  if (value.type !== 'call' || !hasExactKeys(value, 'args,id,op,shape,type')
    || typeof value.shape !== 'string' || value.shape.length > 140
    || typeof value.op !== 'string' || value.op.length > 128) return null;
  return { id: value.id as number, shape: value.shape, op: value.op, args: value.args };
}

/** Bind one fresh MessageChannel to the exact native attachment supplied by Core.
 * No window message handler accepts calls or chooses a principal.
 */
export function mountPackUi(
  container: HTMLElement,
  attachment: UiAttachment,
  transport: UiTransport,
  newChannel: () => MessageChannel = () => new MessageChannel(),
  sharedQueue: UiTransportQueue = new UiTransportQueue(),
  transportNamespace = '',
): IsolatedFrame {
  const url = packUiUrl(attachment);
  const frame = container.ownerDocument.createElement('iframe');
  frame.setAttribute('sandbox', 'allow-scripts');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.className = 'pack-ui-frame';
  frame.title = 'Tobkiri Pack';
  const pending = new Map<number, AbortController>();
  const queue = sharedQueue;
  const queueReceiver = `${transportNamespace}\u0000${attachment.generation}\u0000${attachment.uiSessionId}`;
  let lastId = 0;
  let invalidReplyCount = 0;
  let limitNoticeOutstanding = false;
  let channel: MessageChannel | null = null;
  let closed = false;
  let loaded = false;
  const close = () => {
    if (closed) return;
    closed = true;
    for (const controller of pending.values()) controller.abort();
    pending.clear();
    if (channel) {
      // A closed channel has no further reply capacity. Reject the SDK's
      // remaining requests explicitly before releasing the port.
      try { channel.port1.postMessage({ type: 'tobkiri-pack-closed' }); } catch {}
      channel.port1.onmessage = null;
      channel.port1.close();
      channel.port2.close();
    }
    queue.clearReceiver(queueReceiver);
    frame.remove();
    void transport.close(attachment.uiSessionId, attachment.generation).catch(() => {});
  };
  frame.addEventListener('load', () => {
    if (closed) return;
    // Navigation/reload cannot retain a previous frame's authority or channel.
    if (loaded) { close(); return; }
    loaded = true;
    const target = frame.contentWindow;
    if (!target) { close(); return; }
    channel = newChannel();
    const reply = (value: unknown) => {
      if (closed || !channel) return false;
      try {
        channel.port1.postMessage(value);
        return true;
      } catch {
        return false;
      }
    };
    const rejectInvalid = (id: number | null) => {
      if (invalidReplyCount >= 4) return;
      invalidReplyCount += 1;
      reply({ id, error: { code: 'invalid', message: 'Invalid UI request' } });
    };
    const rejectLimit = (id: number) => {
      // Keep refusal traffic bounded too. A second refusal before a delivered
      // response is ACKed closes the receiver instead of silently stranding
      // an additional guest promise or growing the message queue.
      if (limitNoticeOutstanding) { close(); return; }
      limitNoticeOutstanding = reply({ id, error: { code: 'limit', message: 'UI request refused' } });
    };
    const acknowledge = (id: number) => {
      void Promise.resolve().then(
        () => transport.acknowledge(attachment.uiSessionId, attachment.generation, id),
      )
        .then(() => {
          queue.acknowledge(id, queueReceiver);
          limitNoticeOutstanding = false;
        })
        .catch(() => {});
    };
    channel.port1.onmessage = ({ data }) => {
      if (closed) return;
      const call = message(data);
      if (!call) { rejectInvalid(null); return; }
      if ('close' in call) { close(); return; }
      if ('cancel' in call) { pending.get(call.cancel)?.abort(); return; }
      // The SDK starts at one and increments by exactly one. Reject jumps as
      // well as replays so an untrusted frame cannot exhaust safe-integer IDs
      // or choose host flow-ticket identities through correlation metadata.
      if (call.id !== lastId + 1) {
        rejectInvalid(call.id);
        return;
      }
      // Consume structurally valid IDs even when admission is refused so a
      // saturated receiver cannot replay one expensive oversized frame.
      lastId = call.id;
      const requestBytes = jsonEncodedByteLength(call, HOST_MAX_BODY_BYTES);
      if (requestBytes === null || pending.size >= MAX_PENDING || !queue.reserve(call.id, requestBytes, queueReceiver)) {
        rejectLimit(call.id);
        return;
      }
      let requestJson: string;
      try {
        requestJson = JSON.stringify(call);
      } catch {
        queue.acknowledge(call.id, queueReceiver);
        rejectInvalid(call.id);
        return;
      }
      const controller = new AbortController();
      pending.set(call.id, controller);
      let registered = false;
      let cancelRequest: Promise<void> | null = null;
      const cancel = () => {
        if (!registered) return null;
        if (!cancelRequest) {
          cancelRequest = Promise.resolve().then(
            () => transport.cancel(attachment.uiSessionId, attachment.generation, call),
          );
          // Later aborts signal running work. The pre-dispatch path below also
          // awaits this same ACK, so independent IPC commands cannot reorder it.
          void cancelRequest.catch(() => {});
        }
        return cancelRequest;
      };
      const onAbort = () => cancel();
      controller.signal.addEventListener('abort', onAbort, { once: true });
      const deliver = (result: unknown) => {
        const response = { id: call.id, result: result === undefined ? null : result };
        const responseBytes = jsonEncodedByteLength(response, HOST_MAX_BODY_BYTES);
        const bounded = responseBytes === null
          ? { id: call.id, error: { code: 'limit', message: 'Core response exceeded the UI limit' } }
          : response;
        if (reply(bounded)) acknowledge(call.id);
      };
      const dispatch = async () => {
        if (closed) throw new Error('Pack UI session closed');
        // Registration is a native handshake. Abort during this await is
        // remembered locally; only after its ACK do we send the exact cancel
        // tuple, then dispatch the operation once so the record can retire.
        await transport.register(attachment.uiSessionId, attachment.generation, call);
        registered = true;
        if (controller.signal.aborted) {
          try { await cancel(); }
          catch (error) {
            // A failed cancellation handshake cannot authorize fresh work.
            // Detach retires the registration and signals any running siblings.
            close();
            throw error;
          }
        }
        if (closed) throw new Error('Pack UI session closed');
        return transport.invoke(attachment.uiSessionId, attachment.generation, call, requestJson, controller.signal);
      };
      void Promise.resolve().then(dispatch).then(
        (result) => deliver(result),
        (error) => {
          const coreCode = error && typeof error === 'object' && typeof error.code === 'string' && /^[a-z-]{1,64}$/.test(error.code) ? error.code : 'denied';
          const response = { id: call.id, error: { code: controller.signal.aborted ? 'cancelled' : coreCode, message: 'Core refused the UI request' } };
          if (reply(response)) acknowledge(call.id);
        },
      ).finally(() => {
        controller.signal.removeEventListener('abort', onAbort);
        pending.delete(call.id);
      });
    };
    channel.port1.start();
    // An opaque sandbox origin requires '*'; only this exact Window receives
    // the one-use port. The authority token remains in the trusted closure.
    target.postMessage({ type: 'tobkiri-pack-channel' }, '*', [channel.port2]);
  });
  frame.addEventListener('error', close);
  frame.src = url;
  container.append(frame);
  return { frame, close };
}
