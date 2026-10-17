/** Search filters share this vocabulary across every search entry point. */
export type SearchKind = "chat" | "widget" | "tool" | "model";

export type SearchPreset = {
  kinds?: SearchKind[];
  providerIds?: string[];
  connectionId?: string;
};

export type SearchToken = {
  raw: string;
  axis: "kind" | "provider";
  value: string;
  start: number;
  end: number;
};

export type SearchSuggestion = {
  token: string;
  label: string;
  start: number;
  end: number;
};

export type ParsedSearchQuery = {
  text: string;
  kinds: SearchKind[];
  providerIds: string[];
  connectionId?: string;
  tokens: SearchToken[];
  unknownTokens: string[];
  conflict: boolean;
};

/** Explicit aliases avoid interpreting arbitrary @ text as a provider. */
export const SEARCH_PROVIDER_ALIASES: Readonly<Record<string, string>> = {
  openrouter: "openrouter",
  openai: "openai",
  anthropic: "anthropic",
  google: "google",
  gemini: "google",
  ollama: "ollama",
  deepseek: "deepseek",
  mistral: "mistral",
  groq: "groq",
  xai: "xai",
  perplexity: "perplexity",
  together: "together",
  fireworks: "fireworks",
  cerebras: "cerebras",
  nvidia: "nvidia",
};

export const SEARCH_KIND_LABELS: Readonly<Record<SearchKind, string>> = {
  chat: "会話",
  widget: "ウィジェット",
  tool: "ツール",
  model: "モデル",
};

const PROVIDER_LABELS: Record<string, string> = {
  openrouter: "OpenRouter",
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google",
  gemini: "Gemini",
  ollama: "Ollama",
  deepseek: "DeepSeek",
  mistral: "Mistral",
  groq: "Groq",
  xai: "xAI",
  perplexity: "Perplexity",
  together: "Together",
  fireworks: "Fireworks",
  cerebras: "Cerebras",
  nvidia: "NVIDIA",
};

function isSearchKind(value: string): value is SearchKind {
  return Object.prototype.hasOwnProperty.call(SEARCH_KIND_LABELS, value);
}

function providerAlias(value: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(SEARCH_PROVIDER_ALIASES, value)
    ? SEARCH_PROVIDER_ALIASES[value]
    : undefined;
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)];
}

function intersect<T>(query: T[] | undefined, preset: T[] | undefined): T[] | undefined {
  if (query === undefined) return preset === undefined ? undefined : unique(preset);
  if (preset === undefined) return unique(query);
  const allowed = new Set(preset);
  return unique(query.filter((value) => allowed.has(value)));
}

/** Parse standalone filters, intersecting immutable entry-point constraints. */
export function parseSearchQuery(query: string, preset?: SearchPreset): ParsedSearchQuery {
  const tokens: SearchToken[] = [];
  const unknownTokens: string[] = [];
  const textParts: string[] = [];
  const explicitKinds: SearchKind[] = [];
  const explicitProviders: string[] = [];

  for (const match of query.matchAll(/\S+/gu)) {
    const raw = match[0];
    const start = match.index!;
    const name = raw.startsWith("@") ? raw.slice(1).toLowerCase() : "";
    const provider = providerAlias(name);
    if (isSearchKind(name)) {
      explicitKinds.push(name);
      tokens.push({ raw, axis: "kind", value: name, start, end: start + raw.length });
    } else if (provider) {
      explicitProviders.push(provider);
      tokens.push({ raw, axis: "provider", value: provider, start, end: start + raw.length });
    } else {
      textParts.push(raw);
      if (raw.startsWith("@")) unknownTokens.push(raw);
    }
  }

  const queryKinds = explicitKinds.length > 0
    ? explicitKinds
    : explicitProviders.length > 0 ? ["model" as const] : undefined;
  const presetProviders = preset?.providerIds?.map((id) => {
    const normalized = id.trim().toLowerCase();
    return providerAlias(normalized) ?? normalized;
  });
  const providers = intersect(
    explicitProviders.length > 0 ? explicitProviders : undefined,
    presetProviders,
  );
  const kinds = intersect(queryKinds, preset?.kinds)
    ?? (providers && providers.length > 0 ? ["model"] : ["chat"]);

  return {
    text: textParts.join(" "),
    kinds,
    providerIds: providers ?? [],
    ...(preset?.connectionId !== undefined ? { connectionId: preset.connectionId } : {}),
    tokens,
    unknownTokens,
    conflict: kinds.length === 0 || (providers !== undefined && providers.length === 0),
  };
}

/** Suggest known filters for the standalone @ word containing the caret. */
export function suggestSearchFilters(query: string, caret: number = query.length): SearchSuggestion[] {
  if (!Number.isInteger(caret) || caret < 0 || caret > query.length) return [];
  let start = caret;
  let end = caret;
  while (start > 0 && !/\s/u.test(query[start - 1])) start -= 1;
  while (end < query.length && !/\s/u.test(query[end])) end += 1;
  const prefix = query.slice(start, caret).toLowerCase();
  const word = query.slice(start, end);
  if (!/^@[a-z0-9-]*$/u.test(prefix) || !/^@[a-z0-9-]*$/iu.test(word)) return [];
  const name = prefix.slice(1);
  if (caret === end && (isSearchKind(name) || providerAlias(name))) return [];

  return [
    ...Object.entries(SEARCH_KIND_LABELS).map(([kind, label]) => ({ token: `@${kind}`, label })),
    ...Object.keys(SEARCH_PROVIDER_ALIASES).map((provider) => ({
      token: `@${provider}`,
      label: `${PROVIDER_LABELS[provider] ?? provider} のモデル`,
    })),
  ]
    .filter((item) => item.token.startsWith(prefix))
    .map((item) => ({ ...item, start, end }));
}

function isTokenRange(query: string, start: number, end: number): boolean {
  return Number.isInteger(start) && Number.isInteger(end)
    && start >= 0 && end > start && end <= query.length
    && (start === 0 || /\s/u.test(query[start - 1]))
    && (end === query.length || /\s/u.test(query[end]))
    && /^@\S+$/u.test(query.slice(start, end));
}

/** Replace only the suggested word; retain surrounding text and filters. */
export function applySearchSuggestion(query: string, suggestion: SearchSuggestion): string {
  const { start, end, token } = suggestion;
  const name = token.slice(1).toLowerCase();
  if (!token.startsWith("@") || (!isSearchKind(name) && !providerAlias(name))) return query;
  // A bare @ is also a valid suggestion target.
  const validRange = isTokenRange(query, start, end)
    || (Number.isInteger(start) && end === start + 1 && query[start] === "@"
      && (start === 0 || /\s/u.test(query[start - 1]))
      && (end === query.length || /\s/u.test(query[end])));
  if (!validRange) return query;
  return query.slice(0, start) + token + query.slice(end);
}

/** Remove one parsed filter occurrence only while its offsets still match. */
export function removeSearchToken(query: string, token: SearchToken): string {
  const { start, end, raw } = token;
  if (!isTokenRange(query, start, end) || query.slice(start, end) !== raw) return query;
  const parsedToken = parseSearchQuery(query).tokens.find((candidate) =>
    candidate.start === start && candidate.end === end
      && candidate.axis === token.axis && candidate.value === token.value,
  );
  if (!parsedToken) return query;
  let before = query.slice(0, start);
  let after = query.slice(end);
  if (before.length === 0) after = after.replace(/^\s+/u, "");
  else if (after.length === 0) before = before.replace(/\s+$/u, "");
  else if (/\s$/u.test(before) && /^\s/u.test(after)) after = after.replace(/^\s+/u, "");
  return before + after;
}
