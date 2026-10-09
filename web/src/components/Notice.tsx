import type { ReactNode } from "react";
import { useDelayed } from "../lib/hooks";
import { Button } from "./Button";

export function Notice({ variant = "info", title, children, action }: { variant?: "info" | "attention" | "error"; title?: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className={`notice ${variant === "error" ? "notice--error" : ""}`} role={variant === "error" ? "alert" : "status"}>
      <div>
        {title ? <span className="notice-lead">{title} </span> : null}
        {children}
      </div>
      {action ? <div className="notice-action">{action}</div> : null}
    </div>
  );
}

export function ResultText({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <span className="result-text" role="status">
      {children}
    </span>
  );
}

export function Loading({ what }: { what: string }) {
  const shown = useDelayed(true, 300);
  return <div className="state state--loading">{shown ? `Loading ${what}…` : " "}</div>;
}

export function EmptyState({ children, action, hint }: { children: ReactNode; action?: ReactNode; hint?: ReactNode }) {
  return (
    <div className="state">
      <p className="state-sentence">{children}</p>
      {hint ? <p className="state-hint">{hint}</p> : null}
      {action ? <div className="button-row">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state" role="alert">
      <p className="state-sentence state-error">{message}</p>
      {onRetry ? (
        <div className="button-row">
          <Button onClick={onRetry}>Try again</Button>
        </div>
      ) : null}
    </div>
  );
}
