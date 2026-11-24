/** Compare a JSON acknowledgement with the submitted value, including nested settings. */
export function jsonValueMatches(actual: unknown, expected: unknown): boolean {
  if (actual === expected) return true;
  if (Array.isArray(actual) || Array.isArray(expected)) {
    return Array.isArray(actual) && Array.isArray(expected)
      && actual.length === expected.length
      && actual.every((value, index) => jsonValueMatches(value, expected[index]));
  }
  if (!actual || !expected || typeof actual !== "object" || typeof expected !== "object") return false;
  const actualRecord = actual as Record<string, unknown>;
  const expectedRecord = expected as Record<string, unknown>;
  const keys = Object.keys(expectedRecord);
  return Object.keys(actualRecord).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(actualRecord, key)
      && jsonValueMatches(actualRecord[key], expectedRecord[key]));
}
