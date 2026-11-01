import * as React from "react"
import { cn } from "@/src/lib/utils"

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'destructive' | 'outline' | 'secondary' | 'ghost' | 'link'
  size?: 'default' | 'sm' | 'lg' | 'icon'
  loading?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'default', size = 'default', loading, disabled, children, ...props }, ref) => {
    return (
      <button
        ref={ref}
        {...props}
        aria-busy={loading ? true : props['aria-busy']}
        disabled={disabled || loading}
        className={cn(
          "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-all duration-[var(--transition-fast)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)] disabled:pointer-events-none disabled:opacity-50",
          {
            'bg-accent text-accent-fg shadow-sm hover:bg-accent/90 active:scale-[0.98]': variant === 'default',
            'bg-destructive text-destructive-fg shadow-sm hover:bg-destructive/90 active:scale-[0.98]': variant === 'destructive',
            'border border-border bg-bg-main hover:bg-bg-hover active:bg-bg-hover/80': variant === 'outline',
            'bg-bg-hover text-text-main hover:bg-bg-hover/80': variant === 'secondary',
            'hover:bg-bg-hover text-text-muted hover:text-text-main': variant === 'ghost',
            'text-accent underline-offset-4 hover:underline p-0 h-auto': variant === 'link',
            'h-10 px-4 py-2': size === 'default',
            'h-8 rounded-md px-3 text-xs': size === 'sm',
            'h-11 rounded-lg px-6': size === 'lg',
            'h-9 w-9 p-0': size === 'icon',
          },
          className
        )}
      >
        {loading ? (
          <svg
            aria-hidden="true"
            className="h-4 w-4 shrink-0 animate-spin motion-reduce:animate-none"
            data-loading-indicator="spinner"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        ) : null}
        {children}
      </button>
    )
  }
)
Button.displayName = "Button"

export { Button }
