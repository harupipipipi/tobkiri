import {
  forwardRef, useCallback, useLayoutEffect, useRef, useState,
  type InputHTMLAttributes,
} from "react";
import { parseSearchQuery } from "./searchQuery";

/** A native input with a presentation-only mirror for recognized query filters. */
export const InlineSearchInput = forwardRef<
  HTMLInputElement, InputHTMLAttributes<HTMLInputElement>
>(function InlineSearchInput({
  className = "", value, defaultValue, onChange, onScroll, onSelect,
  onCompositionStart, onCompositionEnd, style, ...props
}, forwardedRef) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const mirrorRef = useRef<HTMLDivElement>(null);
  const [uncontrolledValue, setUncontrolledValue] = useState(String(defaultValue ?? ""));
  const [composing, setComposing] = useState(false);
  const query = String(value ?? uncontrolledValue);
  // Only literal query text is mirrored; immutable search presets stay invisible.
  const tokens = parseSearchQuery(query).tokens;
  const segments = [];
  let offset = 0;
  for (const token of tokens) {
    segments.push(query.slice(offset, token.start));
    segments.push(<span className="inline-search-input__token" data-search-token="" key={token.start}>
      {query.slice(token.start, token.end)}
    </span>);
    offset = token.end;
  }
  segments.push(query.slice(offset));

  const syncScroll = useCallback(() => {
    if (inputRef.current && mirrorRef.current) {
      mirrorRef.current.style.transform = `translateX(${-inputRef.current.scrollLeft}px)`;
    }
  }, []);
  const setInputRef = useCallback((node: HTMLInputElement | null) => {
    inputRef.current = node;
    if (typeof forwardedRef === "function") forwardedRef(node);
    else if (forwardedRef) forwardedRef.current = node;
  }, [forwardedRef]);
  useLayoutEffect(syncScroll, [query, composing, syncScroll]);

  return <div className={`inline-search-input ${className}`} style={style}>
    <div
      aria-hidden="true" data-search-input-overlay=""
      className={`inline-search-input__mirror ${className}`}
      style={style}
      hidden={composing || query.length === 0}
    ><div ref={mirrorRef} data-search-input-scroll="" className="inline-search-input__text">{segments}</div></div>
    <input
      {...props} ref={setInputRef} value={value} defaultValue={defaultValue}
      className={`inline-search-input__native ${className}`}
      style={style}
      data-mirrored={!composing && query.length > 0 ? "true" : undefined}
      onChange={(event) => {
        setUncontrolledValue(event.currentTarget.value);
        onChange?.(event);
        syncScroll();
      }}
      onScroll={(event) => { syncScroll(); onScroll?.(event); }}
      onSelect={(event) => { syncScroll(); onSelect?.(event); }}
      onCompositionStart={(event) => {
        setComposing(true);
        onCompositionStart?.(event);
      }}
      onCompositionEnd={(event) => {
        setComposing(false);
        setUncontrolledValue(event.currentTarget.value);
        onCompositionEnd?.(event);
        syncScroll();
      }}
    />
  </div>;
});
