/** Return whether development-time accessibility contracts should fail fast. */
export function shouldValidateAccessibleContracts(): boolean {
  return import.meta.env?.DEV
    ?? (typeof process !== 'undefined' && process.env.NODE_ENV !== 'production');
}

/** Return whether an explicit ARIA naming relationship is non-empty. */
export function hasExplicitAccessibleName(
  ariaLabel: unknown,
  ariaLabelledBy: unknown,
): boolean {
  return (
    (typeof ariaLabel === 'string' && ariaLabel.trim().length > 0)
    || (typeof ariaLabelledBy === 'string' && ariaLabelledBy.trim().length > 0)
  );
}
