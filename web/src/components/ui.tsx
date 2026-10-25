/** Small shared building blocks used by every page. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { describeError } from "../api/client";
import { copyText } from "../lib/browser";

/** Names the browser tab and history entry after the page ("API keys · Forge"). */
export function useDocumentTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · Forge`;
    return () => {
      document.title = "Forge";
    };
  }, [title]);
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  useDocumentTitle(title);
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p className="page-desc">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="banner banner-error" role="alert">
      <span>{describeError(error)}</span>
      {onRetry && (
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function CopyButton({ text, label = "Copy", className = "" }: { text: string; label?: string; className?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onClick = async () => {
    try {
      await copyText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1600);
  };

  return (
    <button type="button" className={`btn btn-small ${className}`} onClick={() => void onClick()} aria-live="polite">
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : label}
    </button>
  );
}

/** Horizontal usage bar: used / limit, with the numbers spelled out next to it. */
export function Meter({ value, max, warnAt = 0.8 }: { value: number; max: number; warnAt?: number }) {
  const fraction = max > 0 ? Math.min(1, value / max) : 0;
  const level = fraction >= 1 ? "full" : fraction >= warnAt ? "warn" : "ok";
  return (
    <span className={`meter meter-${level}`} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      <span className="meter-fill" style={{ width: `${fraction * 100}%` }} />
    </span>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
