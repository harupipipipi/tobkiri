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
  ({ className, type, label, helperText, error, id, ...props }, ref) => {
    const inputId = id || (label ? `input-${label.toLowerCase().replace(/\s+/g, '-')}` : undefined);

    const input = (
      <input
        id={inputId}
        type={type}
        className={cn(
          "flex h-10 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main placeholder:text-text-muted transition-colors duration-[var(--transition-fast)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)] disabled:cursor-not-allowed disabled:opacity-50",
          error && "border-destructive focus-visible:ring-destructive/40",
          className
        )}
        ref={ref}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={error ? `${inputId}-error` : helperText ? `${inputId}-helper` : undefined}
        {...props}
      />
    );

    if (!label && !helperText && !error) return input;

    return (
      <div className="space-y-1.5">
        {label && (
          <label htmlFor={inputId} className="text-sm font-medium text-text-main">
            {label}
            {props.required && <span className="ml-1 text-destructive">*</span>}
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
        {!error && helperText && (
          <p id={`${inputId}-helper`} className="text-xs text-text-muted">{helperText}</p>
        )}
      </div>
    );
  }
)
Input.displayName = "Input"

export { Input }
