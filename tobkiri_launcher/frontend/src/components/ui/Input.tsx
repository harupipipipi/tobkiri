import * as React from "react"
import {AlertCircle} from 'lucide-react'
import { cn } from "@/src/lib/utils"
import {CopyErrorButton} from './CopyErrorButton'

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  helperText?: string;
  error?: string;
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({
    className,
    type,
    label,
    helperText,
    error,
    id,
    'aria-describedby': ariaDescribedBy,
    'aria-invalid': ariaInvalid,
    ...props
  }, ref) => {
    const generatedId = React.useId();
    const inputId = id ?? `input-${generatedId}`;
    const helperId = helperText ? `${inputId}-helper` : undefined;
    const errorId = error ? `${inputId}-error` : undefined;
    const describedBy = [ariaDescribedBy, helperId, errorId]
      .filter(Boolean)
      .join(' ') || undefined;

    const input = (
      <input
        id={inputId}
        type={type}
        {...props}
        className={cn(
          "flex h-10 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main placeholder:text-text-muted transition-colors duration-[var(--transition-fast)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)] disabled:cursor-not-allowed disabled:opacity-50",
          error && "border-destructive focus-visible:ring-destructive/40",
          className
        )}
        ref={ref}
        aria-invalid={error ? "true" : ariaInvalid}
        aria-describedby={describedBy}
      />
    );

    if (!label && !helperText && !error) return input;

    return (
      <div className="space-y-1.5">
        {label && (
          <label htmlFor={inputId} className="text-sm font-medium text-text-main">
            {label}
            {props.required && <span aria-hidden="true" className="ml-1 text-destructive">*</span>}
          </label>
        )}
        {input}
        {error && (
          <div className="flex items-start gap-2" id={`${inputId}-error`} role="alert">
            <AlertCircle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
            <p className="min-w-0 flex-1 break-words text-xs text-destructive">{error}</p>
            <CopyErrorButton label={`Copy ${label ?? 'input'} error`} text={error} />
          </div>
        )}
        {error && (
          <p id={errorId} role="alert" className="text-xs text-destructive">{error}</p>
        )}
      </div>
    );
  }
)
Input.displayName = "Input"

export { Input }
