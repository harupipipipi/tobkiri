import * as React from "react"
import { cn } from "@/src/lib/utils"
import {
  hasExplicitAccessibleName,
  shouldValidateAccessibleContracts,
} from './controlAccessibility';

type SwitchAccessibleName =
  | { 'aria-label': string; 'aria-labelledby'?: never }
  | { 'aria-label'?: never; 'aria-labelledby': string };

export type SwitchProps = Omit<
  React.ButtonHTMLAttributes<HTMLButtonElement>,
  'aria-checked' | 'aria-label' | 'aria-labelledby' | 'role' | 'type'
> & SwitchAccessibleName & {
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
};

const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  ({ className, checked = false, onCheckedChange, onClick, ...props }, ref) => {
    if (
      shouldValidateAccessibleContracts()
      && !hasExplicitAccessibleName(props['aria-label'], props['aria-labelledby'])
    ) {
      throw new Error('Switch requires aria-label or aria-labelledby.');
    }

    /**
     * Native button activation normalizes pointer, Enter, and Space to click.
     * The caller runs first and may cancel the owned state transition with
     * preventDefault().
     */
    const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
      onClick?.(event);
      if (!event.defaultPrevented) {
        onCheckedChange?.(!checked);
      }
    };

    return (
      <button
        ref={ref}
        {...props}
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={handleClick}
        className={cn(
          "peer inline-flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)] disabled:cursor-not-allowed disabled:opacity-50",
          className
        )}
      >
        <span
          aria-hidden="true"
          className={cn(
            "pointer-events-none flex h-6 w-11 items-center rounded-full border-2 border-transparent transition-colors duration-[var(--transition-fast)]",
            checked ? "bg-accent" : "bg-border"
          )}
        >
          <span
            className={cn(
              "block h-5 w-5 rounded-full bg-white shadow-sm ring-0 transition-transform duration-[var(--transition-fast)]",
              checked ? "translate-x-5" : "translate-x-0"
            )}
          />
        </span>
      </button>
    )
  }
)
Switch.displayName = "Switch"

export { Switch }
