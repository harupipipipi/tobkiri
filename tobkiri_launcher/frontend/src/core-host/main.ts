import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { appendBoundedHistory, HOST_MAX_BODY_BYTES, HOST_MAX_INFLIGHT, HostOutputWatermark, UiTransportQueue, utf8ByteLength } from './boundedTransport';
import { mountPackUi, type IsolatedFrame, type UiAttachment, type UiRequest } from './isolatedPackUi';
import { ProfileStopController } from './profileStop';
import './style.css';

const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const status = element('status');
const terminal = element('terminal-output');
const terminalForm = element<HTMLFormElement>('terminal-form');
let frames: IsolatedFrame[] = [];
let terminalRequest: string | null = null;
let terminalReplyMaxBytes = 65536;
let generation = 0;
let activeCoreGeneration = 0;
let coreStartPending = false;
type HostOutputEvent = { namespace: string; generation: number; receiver: string; ticketId: number; surface: string | null; payload: string };
type HostTicket = { namespace: string; generation: number; receiver: string; ticketId: number };
let pendingHostOutput: HostOutputEvent | null = null;
let activeTransportNamespace = '';
const hostOutputWatermark = new HostOutputWatermark();
const MAX_HISTORY_CODE_UNITS = 262144;
const MAX_EVENT_TEXT_BYTES = HOST_MAX_BODY_BYTES;
const uiTransportQueue = new UiTransportQueue();

const closeFrames = () => { for (const frame of frames) frame.close(); frames = []; };
const boundedJsonParse = (json: string) => {
  if (utf8ByteLength(json, HOST_MAX_BODY_BYTES) === null) throw Object.assign(new Error('Core response exceeded the UI limit'), { code: 'limit' });
  return JSON.parse(json) as unknown;
};

await listen<{ text: string }>('tobkiri-core-terminal', ({ payload }) => {
  if (typeof payload.text !== 'string' || utf8ByteLength(payload.text, MAX_EVENT_TEXT_BYTES) === null) return;
  terminal.textContent = appendBoundedHistory(terminal.textContent ?? '', payload.text, MAX_HISTORY_CODE_UNITS);
});
const consumeHostOutput = (payload: HostOutputEvent) => {
  if (payload.namespace !== activeTransportNamespace
    || payload.receiver !== 'core-host'
    || payload.generation !== activeCoreGeneration
    || !hostOutputWatermark.accept(payload.namespace, payload.ticketId)) return;
  if (payload.surface !== null) {
    terminal.textContent = appendBoundedHistory(terminal.textContent ?? '', `${payload.surface}: `, MAX_HISTORY_CODE_UNITS);
  }
  terminal.textContent = appendBoundedHistory(terminal.textContent ?? '', payload.payload, MAX_HISTORY_CODE_UNITS);
  void invoke('core_host_output_ack', {
    namespace: payload.namespace,
    generation: payload.generation,
    receiver: payload.receiver,
    ticketId: payload.ticketId,
  }).catch(() => {});
};
await listen<HostOutputEvent>('tobkiri-core-output', ({ payload }) => {
  if (!payload || typeof payload !== 'object') return;
  const surfaceBytes = payload.surface === null ? 0
    : typeof payload.surface === 'string' ? utf8ByteLength(payload.surface, 256) : null;
  const bodyBytes = typeof payload.payload === 'string'
    ? utf8ByteLength(payload.payload, MAX_EVENT_TEXT_BYTES) : null;
  if (typeof payload.namespace !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(payload.namespace)
    || !Number.isSafeInteger(payload.generation) || payload.generation < 1
    || payload.receiver !== 'core-host'
    || !Number.isSafeInteger(payload.ticketId) || payload.ticketId < 1
    || surfaceBytes === null || bodyBytes === null
    || surfaceBytes + bodyBytes > MAX_EVENT_TEXT_BYTES) return;
  if (activeCoreGeneration === 0 && coreStartPending) {
    if (pendingHostOutput === null) pendingHostOutput = payload;
    return;
  }
  consumeHostOutput(payload);
});
await listen<{ id: string; prompt: string; maxBytes: number }>('tobkiri-core-terminal-read', ({ payload }) => {
  if (typeof payload.id !== 'string' || typeof payload.prompt !== 'string'
    || !Number.isSafeInteger(payload.maxBytes) || payload.maxBytes < 0 || payload.maxBytes > 65536
    || utf8ByteLength(payload.id, 128) === null || utf8ByteLength(payload.prompt, 4096) === null) return;
  terminalRequest = payload.id;
  terminalReplyMaxBytes = payload.maxBytes;
  element('terminal-prompt').textContent = payload.prompt;
  terminalForm.hidden = false;
  element<HTMLInputElement>('terminal-input').focus();
});
await listen<string>('tobkiri-core-terminal-close', ({ payload }) => {
  if (terminalRequest === payload) { terminalRequest = null; terminalForm.hidden = true; }
});

terminalForm.addEventListener('submit', (event) => {
  event.preventDefault();
  if (!terminalRequest) return;
  const input = element<HTMLInputElement>('terminal-input');
  if (utf8ByteLength(input.value, terminalReplyMaxBytes) === null) {
    status.textContent = 'Terminal response is too large.';
    return;
  }
  const id = terminalRequest;
  terminalRequest = null;
  terminalForm.hidden = true;
  void invoke('core_terminal_reply', { id, text: input.value }).catch(() => { status.textContent = 'Terminal request expired.'; });
  input.value = '';
});

const profileStop = new ProfileStopController(status, element<HTMLButtonElement>('stop'), {
  getGeneration: () => generation,
  advanceGeneration: () => ++generation,
  prepareStop: () => {
    closeFrames();
    activeCoreGeneration = 0;
    activeTransportNamespace = '';
    coreStartPending = false;
    pendingHostOutput = null;
    terminalRequest = null;
    terminalForm.hidden = true;
  },
  invokeStop: () => invoke('core_stop_session'),
});

element<HTMLFormElement>('profile-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const current = ++generation;
  profileStop.resetForStart();
  closeFrames();
  activeCoreGeneration = 0;
  activeTransportNamespace = '';
  coreStartPending = true;
  pendingHostOutput = null;
  status.textContent = 'Starting Profile…';
  const profile = element<HTMLInputElement>('profile-id').value;
  void invoke<{ namespace: string; generation: number; attachments: UiAttachment[] }>('core_start_profile', { profile }).then((result) => {
    if (current !== generation) return;
    if (typeof result.namespace !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(result.namespace)
      || !Number.isSafeInteger(result.generation) || result.generation < 1 || !Array.isArray(result.attachments)) {
      throw new Error('Invalid Core session attachment');
    }
    activeTransportNamespace = result.namespace;
    hostOutputWatermark.useNamespace(result.namespace);
    activeCoreGeneration = result.generation;
    coreStartPending = false;
    const attachments = result.attachments;
    if (attachments.some((attachment) => attachment.generation !== result.generation)) {
      throw new Error('Mismatched Core session generation');
    }
    frames = attachments.map((attachment) => {
      // The iframe's request ID is correlation metadata only. The ticket ID
      // comes from the trusted Launcher and is never forwarded to the frame.
      const ticketIds = new Map<number, HostTicket>();
      const rememberTicket = (requestId: number, ticket: HostTicket) => {
        if (ticket.namespace !== result.namespace
          || ticket.generation !== attachment.generation
          || ticket.receiver !== attachment.uiSessionId
          || !Number.isSafeInteger(ticket.ticketId) || ticket.ticketId < 1
          || (ticketIds.size >= HOST_MAX_INFLIGHT && !ticketIds.has(requestId))) {
          throw new Error('Invalid host transport ticket');
        }
        ticketIds.set(requestId, {
          namespace: ticket.namespace,
          generation: ticket.generation,
          receiver: ticket.receiver,
          ticketId: ticket.ticketId,
        });
      };
      const payload = (reply: HostTicket & { payload: string }, requestId: number) => {
        if (!reply || typeof reply !== 'object' || reply.namespace !== result.namespace
          || reply.generation !== attachment.generation || reply.receiver !== attachment.uiSessionId
          || typeof reply.payload !== 'string') {
          throw new Error('Invalid host transport response');
        }
        rememberTicket(requestId, reply);
        return boundedJsonParse(reply.payload);
      };
      let registrationTail: Promise<void> = Promise.resolve();
      const operationFields = (call: UiRequest) => {
        if (!('type' in call)) return { kind: 'invoke', handle: null };
        if (call.type === 'stream-pull') return { kind: 'stream-pull', handle: call.handle };
        return { kind: 'stream-cancel', handle: call.handle };
      };
      return mountPackUi(element('pack-ui'), attachment, {
        register: (uiSessionId, sessionGeneration, call) => {
          const fields = operationFields(call);
          const registration = registrationTail.then(async () => {
            const ack = await invoke<{
              namespace: string; generation: number; receiver: string; id: number; kind: string; handle: number | null;
            }>('pack_ui_register', {
              namespace: result.namespace,
              uiSessionId,
              generation: sessionGeneration,
              id: call.id,
              kind: fields.kind,
              handle: fields.handle,
            });
            if (!ack || ack.namespace !== result.namespace || ack.generation !== sessionGeneration
              || ack.receiver !== uiSessionId || ack.id !== call.id || ack.kind !== fields.kind
              || ack.handle !== fields.handle) {
              throw Object.assign(new Error('Invalid operation registration ACK'), { code: 'invalid' });
            }
          });
          registrationTail = registration.catch(() => {});
          return registration;
        },
        invoke: async (uiSessionId, sessionGeneration, call, requestJson, _signal) => {
          if ('type' in call && call.type === 'stream-pull') {
            const pending = invoke<HostTicket & { payload: string }>('pack_ui_stream_pull', {
              namespace: result.namespace, uiSessionId, generation: sessionGeneration, id: call.id, handle: call.handle, max: call.max,
            });
            return payload(await pending, call.id);
          }
          if ('type' in call && call.type === 'stream-cancel') {
            const ticket = await invoke<HostTicket | null>('pack_ui_stream_cancel', {
              namespace: result.namespace, uiSessionId, generation: sessionGeneration, id: call.id, handle: call.handle, abortPull: false,
            });
            if (ticket === null) throw new Error('Stream cancellation did not receive a flow ticket');
            rememberTicket(call.id, ticket);
            return null;
          }
          const pending = invoke<HostTicket & { payload: string }>('pack_ui_invoke', {
            namespace: result.namespace, uiSessionId, generation: sessionGeneration, id: call.id, requestJson,
          });
          return payload(await pending, call.id);
        },
        cancel: async (uiSessionId, sessionGeneration, call) => {
          const fields = operationFields(call);
          if (fields.kind === 'stream-pull') {
            await invoke('pack_ui_stream_cancel', {
              namespace: result.namespace,
              uiSessionId,
              generation: sessionGeneration,
              id: call.id,
              handle: fields.handle,
              abortPull: true,
            });
            return;
          }
          await invoke('pack_ui_cancel', {
            namespace: result.namespace,
            uiSessionId,
            generation: sessionGeneration,
            id: call.id,
            kind: fields.kind,
            handle: fields.handle,
          });
        },
        acknowledge: async (uiSessionId, sessionGeneration, requestId) => {
          const ticket = ticketIds.get(requestId);
          if (ticket === undefined) return;
          if (uiSessionId !== ticket.receiver || sessionGeneration !== ticket.generation) {
            throw new Error('Mismatched host ticket receiver');
          }
          await invoke('pack_ui_ack', {
            namespace: ticket.namespace,
            uiSessionId: ticket.receiver,
            generation: ticket.generation,
            ticketId: ticket.ticketId,
          });
          ticketIds.delete(requestId);
        },
        close: async (uiSessionId, sessionGeneration) => {
          if (uiSessionId !== attachment.uiSessionId || sessionGeneration !== attachment.generation) {
            throw new Error('Mismatched Core UI attachment');
          }
          await registrationTail;
          ticketIds.clear();
          await invoke('pack_ui_detach', { namespace: result.namespace, uiSessionId, generation: sessionGeneration });
        },
      }, undefined, uiTransportQueue, result.namespace);
    });
    status.textContent = attachments.length ? 'Profile running.' : 'Profile running without an embedded UI.';
    if (pendingHostOutput) {
      const pending = pendingHostOutput;
      pendingHostOutput = null;
      consumeHostOutput(pending);
    }
  }).catch(() => {
    if (current !== generation) return;
    activeCoreGeneration = 0;
    activeTransportNamespace = '';
    coreStartPending = false;
    pendingHostOutput = null;
    status.textContent = 'Core could not start this Profile.';
  });
});

element('stop').addEventListener('click', profileStop.stop);
window.addEventListener('pagehide', profileStop.stop, { once: true });
// The module awaits native event registration before installing the handlers.
// Do not accept a user submission while that initialization is still pending.
element<HTMLButtonElement>('start').disabled = false;
