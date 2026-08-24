export type ToastType = 'success' | 'info' | 'warning' | 'error';

export interface ToastAction {
  label: string;
  onAction: () => void | Promise<void>;
}

export interface ToastOptions {
  action?: ToastAction;
  dedupeKey?: string;
  durationMs?: number;
  persistent?: boolean;
  replacementKey?: string;
}

export interface Toast {
  id: string;
  message: string;
  type: ToastType;
  action?: ToastAction;
  dedupeKey: string;
  durationMs: number;
  persistent: boolean;
  replacementKey?: string;
  revision: number;
}

export type ToastUpdate = Partial<Pick<
  Toast,
  'message' | 'type' | 'action' | 'durationMs' | 'persistent'
>>;

export const MAX_QUEUED_TOASTS = 5;

function defaultDuration(type: ToastType): number {
  if (type === 'error') return 12_000;
  if (type === 'warning') return 10_000;
  return 6_000;
}

function normalizedDuration(
  type: ToastType,
  action: ToastAction | undefined,
  requested: number | undefined,
): number {
  const candidate = requested ?? defaultDuration(type);
  const finiteCandidate = Number.isFinite(candidate) ? candidate : defaultDuration(type);
  return Math.min(60_000, Math.max(action ? 15_000 : 1_000, finiteCandidate));
}

/** Add, deduplicate, or replace a notification in the bounded queue. */
export function enqueueToast(
  current: Toast[],
  message: string,
  type: ToastType,
  options: ToastOptions,
  createId: () => string,
): Toast[] {
  const normalizedMessage = message.trim();
  if (!normalizedMessage) return current;
  const dedupeKey = options.dedupeKey?.trim() || `${type}:${normalizedMessage}`;
  const replacementKey = options.replacementKey?.trim() || undefined;
  const durationMs = normalizedDuration(type, options.action, options.durationMs);
  const replacementIndex = replacementKey
    ? current.findIndex((toast) => toast.replacementKey === replacementKey)
    : -1;
  if (replacementIndex >= 0) {
    const previous = current[replacementIndex];
    const toasts = [...current];
    toasts[replacementIndex] = {
      id: previous.id,
      message: normalizedMessage,
      type,
      action: options.action,
      dedupeKey,
      durationMs,
      persistent: options.persistent ?? false,
      replacementKey,
      revision: previous.revision + 1,
    };
    return toasts;
  }
  if (current.some((toast) => toast.dedupeKey === dedupeKey)) return current;
  const toast: Toast = {
    id: createId(),
    message: normalizedMessage,
    type,
    action: options.action,
    dedupeKey,
    durationMs,
    persistent: options.persistent ?? false,
    replacementKey,
    revision: 0,
  };
  return [...current, toast].slice(-MAX_QUEUED_TOASTS);
}

/** Update one queued notification in place so assistive technology hears one change. */
export function updateQueuedToast(
  current: Toast[],
  id: string,
  update: ToastUpdate,
): Toast[] {
  return current.map((toast) => {
    if (toast.id !== id) return toast;
    const message = update.message?.trim() || toast.message;
    const type = update.type ?? toast.type;
    const action = update.action ?? toast.action;
    return {
      ...toast,
      ...update,
      message,
      type,
      action,
      dedupeKey: `${type}:${message}`,
      durationMs: normalizedDuration(
        type,
        action,
        update.durationMs ?? toast.durationMs,
      ),
      revision: toast.revision + 1,
    };
  });
}
