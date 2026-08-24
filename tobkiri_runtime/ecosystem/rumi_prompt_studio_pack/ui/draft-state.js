(() => {
"use strict";
const DRAFT_NAMESPACE = "tobkiri.prompt-studio.draft.v1";
const HISTORY_STORAGE_KEY = "tobkiriPromptStudioDraftVaultV1";
const MAX_DRAFT_AGE_MS = 30 * 24 * 60 * 60 * 1000;

const text = (value) => String(value ?? "").trim();

const contextIdentity = (context = {}) => ({
  profileId: text(context.profileId),
  promptId: text(context.promptId),
  modelProfileId: text(context.modelProfileId),
  conversationId: text(context.conversationId),
  baseRevision: text(context.baseRevision),
});

const contextKey = (context = {}) => JSON.stringify(contextIdentity(context));

const draftStorageKey = (context = {}) => (
  `${DRAFT_NAMESPACE}:${encodeURIComponent(contextKey(context))}`
);

const isDirtyDraft = (baseBody, draftBody) => (
  String(baseBody ?? "") !== String(draftBody ?? "")
);

const preserveEditorState = (state = {}) => Object.freeze({
  selectionStart: Number(state.selectionStart ?? 0),
  selectionEnd: Number(state.selectionEnd ?? 0),
  editorScrollTop: Number(state.editorScrollTop ?? 0),
  pageScrollX: Number(state.pageScrollX ?? 0),
  pageScrollY: Number(state.pageScrollY ?? 0),
  output: String(state.output ?? ""),
  outputScrollTop: Number(state.outputScrollTop ?? 0),
  versionsExpanded: Boolean(state.versionsExpanded),
});

const shouldBlockUnload = (dirty, suppressed = false) => Boolean(dirty) && !suppressed;

class HistoryStateStorage {
  constructor(historyAdapter) {
    this.history = historyAdapter;
  }

  values() {
    const state = this.history?.state;
    if (!state || typeof state !== "object" || Array.isArray(state)) return {};
    const values = state[HISTORY_STORAGE_KEY];
    return values && typeof values === "object" && !Array.isArray(values)
      ? { ...values }
      : {};
  }

  replace(values) {
    const state = this.history?.state;
    const base = state && typeof state === "object" && !Array.isArray(state)
      ? state
      : {};
    this.history?.replaceState({ ...base, [HISTORY_STORAGE_KEY]: values }, "");
  }

  getItem(key) {
    const value = this.values()[key];
    return typeof value === "string" ? value : null;
  }

  setItem(key, value) {
    this.replace({ ...this.values(), [key]: String(value) });
  }

  removeItem(key) {
    const values = this.values();
    delete values[key];
    this.replace(values);
  }
}

class DraftVault {
  constructor(storage, now = () => Date.now()) {
    this.storage = storage;
    this.now = now;
  }

  save(context, draftBody, uiState = {}) {
    const identity = contextIdentity(context);
    if (!identity.profileId || !identity.promptId || !identity.baseRevision) return null;
    const record = {
      version: DRAFT_NAMESPACE,
      context: identity,
      draftBody: String(draftBody ?? ""),
      uiState: preserveEditorState(uiState),
      updatedAt: this.now(),
    };
    try {
      this.storage?.setItem(draftStorageKey(identity), JSON.stringify(record));
      return record;
    } catch {
      return null;
    }
  }

  restore(context) {
    const identity = contextIdentity(context);
    try {
      const raw = this.storage?.getItem(draftStorageKey(identity));
      if (!raw) return null;
      const record = JSON.parse(raw);
      if (
        record?.version !== DRAFT_NAMESPACE
        || contextKey(record.context) !== contextKey(identity)
        || !Number.isFinite(record.updatedAt)
        || this.now() - record.updatedAt > MAX_DRAFT_AGE_MS
      ) {
        this.remove(identity);
        return null;
      }
      return record;
    } catch {
      return null;
    }
  }

  remove(context) {
    try {
      this.storage?.removeItem(draftStorageKey(context));
    } catch {
      // Storage is best-effort; authoring must remain usable in privacy modes.
    }
  }
}

class LoadSequencer {
  constructor() {
    this.generation = 0;
    this.latest = null;
  }

  begin(context) {
    this.latest = { generation: ++this.generation, context: contextKey(context) };
    return { ...this.latest };
  }

  accepts(token, context) {
    return Boolean(
      token
      && this.latest
      && token.generation === this.latest.generation
      && token.context === this.latest.context
      && token.context === contextKey(context),
    );
  }
}

const transitionNeedsResolution = (kind, dirty) => (
  Boolean(dirty) && new Set([
    "prompt", "profile", "model", "conversation", "refresh", "back",
    "route", "rollback",
  ]).has(String(kind))
);

const conflictChoice = (choice) => {
  const normalized = String(choice || "");
  if (["compare", "reload", "overwrite", "cancel"].includes(normalized)) {
    return normalized;
  }
  throw new Error("invalid conflict choice");
};

globalThis.PromptStudioDraftState = Object.freeze({
  DraftVault,
  HistoryStateStorage,
  LoadSequencer,
  conflictChoice,
  contextIdentity,
  contextKey,
  draftStorageKey,
  isDirtyDraft,
  preserveEditorState,
  shouldBlockUnload,
  transitionNeedsResolution,
});
})();
