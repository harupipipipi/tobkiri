import * as React from "react"
import { createPortal } from "react-dom"
import { cn } from "@/src/lib/utils"
import { viewerLayers } from "@/src/lib/layers"

const Popover = ({ children }: { children: React.ReactNode }) => {
  const [isOpen, setIsOpen] = React.useState(false)
  const contentId = React.useId()
  const triggerRef = React.useRef<HTMLButtonElement>(null)
  const contentRef = React.useRef<HTMLDivElement>(null)
  const close = React.useCallback(() => {
    setIsOpen(false)
    window.setTimeout(() => triggerRef.current?.focus(), 0)
  }, [])

  React.useEffect(() => {
    if (!isOpen) return

    const timer = window.setTimeout(() => {
      const firstFocusable = contentRef.current?.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
      firstFocusable?.focus()
    }, 0)

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (
        contentRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return
      }
      close()
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        close()
      }
    }

    document.addEventListener("pointerdown", handlePointerDown)
    window.addEventListener("keydown", handleKeyDown)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener("pointerdown", handlePointerDown)
      window.removeEventListener("keydown", handleKeyDown)
    }
  }, [isOpen, close])

  return (
    <div className="relative inline-block">
      {React.Children.map(children, (child) => {
        if (React.isValidElement(child)) {
          if (child.type === PopoverTrigger) {
            return React.cloneElement(child as React.ReactElement<any>, {
              ref: triggerRef,
              onClick: () => setIsOpen((open) => !open),
              isOpen,
              'aria-controls': isOpen ? contentId : undefined,
            })
          }
          if (child.type === PopoverContent) {
            return isOpen ? React.cloneElement(child as React.ReactElement<any>, {
              ref: contentRef,
              onClose: close,
              triggerRef,
              id: contentId,
            }) : null
          }
        }
        return child
      })}
    </div>
  )
}

type PopoverTriggerProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  isOpen?: boolean;
};

const PopoverTrigger = React.forwardRef<HTMLButtonElement, PopoverTriggerProps>(
  ({ children, onClick, className, isOpen, type = "button", ...props }, ref) => (
  <button
    ref={ref}
    type={type}
    onClick={onClick}
    aria-haspopup={props['aria-haspopup'] ?? 'menu'}
    aria-expanded={Boolean(isOpen)}
    className={cn("cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]", className)}
    {...props}
  >
    {children}
  </button>
))
PopoverTrigger.displayName = "PopoverTrigger"

type PopoverContentProps = React.HTMLAttributes<HTMLDivElement> & {
  align?: "left" | "right";
  onClose?: () => void;
  triggerRef?: React.RefObject<HTMLButtonElement>;
};

const PopoverContent = React.forwardRef<HTMLDivElement, PopoverContentProps>(
  ({ children, className, align = "right", onClose, onClick, role = "menu", triggerRef, style, ...props }, ref) => {
  const localRef = React.useRef<HTMLDivElement | null>(null)
  const getPosition = React.useCallback(() => {
    const trigger = triggerRef?.current
    if (!trigger) return { top: 0, left: 0, transform: undefined as string | undefined }

    const rect = trigger.getBoundingClientRect()
    const gap = 8
    const viewportPadding = 8
    const viewportHeight = window.innerHeight
    const viewportWidth = window.innerWidth
    const content = localRef.current
    const contentHeight = content?.offsetHeight ?? 0
    const contentWidth = content?.offsetWidth ?? 0

    // Flip above the trigger when the menu would not fit below it. The sidebar
    // profile menu is anchored at the bottom of the window, so without this it
    // opens past the viewport edge and cannot be reached.
    const spaceBelow = viewportHeight - rect.bottom - gap - viewportPadding
    const openAbove = contentHeight > 0
      && spaceBelow < contentHeight
      && rect.top - gap - viewportPadding > spaceBelow
    const top = openAbove
      ? Math.max(viewportPadding, rect.top - gap - contentHeight)
      : Math.max(viewportPadding, Math.min(rect.bottom + gap, viewportHeight - viewportPadding - contentHeight))

    const rawLeft = align === "right" ? rect.right : rect.left
    // `transform: translateX(-100%)` makes `left` the menu's right edge, so the
    // clamp bounds differ per alignment.
    const left = align === "right"
      ? Math.min(viewportWidth - viewportPadding, Math.max(viewportPadding + contentWidth, rawLeft))
      : Math.max(viewportPadding, Math.min(rawLeft, viewportWidth - viewportPadding - contentWidth))

    return {
      top,
      left,
      transform: align === "right" ? "translateX(-100%)" : undefined,
    }
  }, [align, triggerRef])
  const [position, setPosition] = React.useState(getPosition)

  const setRefs = React.useCallback((node: HTMLDivElement | null) => {
    localRef.current = node
    if (typeof ref === "function") {
      ref(node)
    } else if (ref) {
      ref.current = node
    }
  }, [ref])

  const updatePosition = React.useCallback(() => {
    setPosition(getPosition())
  }, [getPosition])

  React.useLayoutEffect(() => {
    updatePosition()
  }, [updatePosition])

  React.useEffect(() => {
    updatePosition()
    window.addEventListener("resize", updatePosition)
    window.addEventListener("scroll", updatePosition, true)
    return () => {
      window.removeEventListener("resize", updatePosition)
      window.removeEventListener("scroll", updatePosition, true)
    }
  }, [updatePosition])

  return createPortal(
    <div
      ref={setRefs}
      role={role}
      tabIndex={-1}
      onClick={(event) => {
        onClick?.(event)
        if ((event.target as HTMLElement).closest('a,button')) {
          onClose?.()
        }
      }}
      style={{
        position: "fixed",
        top: position.top,
        left: position.left,
        transform: position.transform,
        ...style,
      }}
      className={cn(
        "min-w-[12rem] overflow-hidden rounded-xl border border-border bg-bg-card p-1 text-text-main shadow-[var(--shadow-lg)] outline-none",
        viewerLayers.popover,
        className
      )}
      {...props}
    >
      {children}
    </div>,
    document.body,
  )
})
PopoverContent.displayName = "PopoverContent"

export { Popover, PopoverTrigger, PopoverContent }
