import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";
import { IconButton } from "./Button";
import { Notice } from "./Notice";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function useFocusTrap(ref: React.RefObject<HTMLElement | null>, active: boolean, initial?: string): void {
  useEffect(() => {
    if (!active) return;
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current;
    if (node) {
      const target = (initial ? node.querySelector<HTMLElement>(initial) : null) ?? node.querySelector<HTMLElement>("[data-autofocus]") ?? node.querySelector<HTMLElement>(FOCUSABLE);
      window.setTimeout(() => (target ?? node).focus(), 0);
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !ref.current) return;
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previous && document.contains(previous)) previous.focus();
    };
  }, [ref, active, initial]);
}

interface DialogProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  alert?: boolean;
  busy?: boolean;
  onSubmit?: () => void;
  initialFocus?: string;
  describedBy?: string;
  bottomSheet?: boolean;
}

export function Dialog({ title, onClose, children, footer, wide, alert, busy, onSubmit, initialFocus, describedBy, bottomSheet }: DialogProps) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(ref, true, initialFocus);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [busy, onClose]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!busy) onSubmit?.();
  };
  const body = (
    <>
      <div className="dialog-head">
        <h2 id={titleId} className="dialog-title">
          {title}
        </h2>
        <IconButton icon="close" label="Close" shortcut="Escape" onClick={() => !busy && onClose()} disabled={busy} />
      </div>
      <div className="dialog-body">{children}</div>
      {footer ? <div className="dialog-foot">{footer}</div> : null}
    </>
  );
  return createPortal(
    <div className="overlay" data-overlay-open="">
      <div className="scrim" onClick={() => !busy && onClose()} />
      <div
        ref={ref}
        role={alert ? "alertdialog" : "dialog"}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={describedBy}
        className={`dialog ${wide ? "dialog--wide" : ""} ${bottomSheet ? "dialog--sheet" : ""}`}
        tabIndex={-1}
      >
        {onSubmit ? (
          <form onSubmit={submit} noValidate>
            {body}
          </form>
        ) : (
          body
        )}
      </div>
    </div>,
    document.body,
  );
}

interface ConfirmProps {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  onConfirm: () => Promise<unknown> | unknown;
  onClose: () => void;
  typeToConfirm?: string;
  destructive?: boolean;
  errorFor?: (err: unknown) => string;
  extra?: ReactNode;
  extraValid?: boolean;
  cancelLabel?: string;
}

export function ConfirmDialog({ title, body, confirmLabel, busyLabel, onConfirm, onClose, typeToConfirm, destructive = true, errorFor, extra, extraValid = true, cancelLabel = "Cancel" }: ConfirmProps) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bodyId = useId();
  const typeId = useId();
  const matches = !typeToConfirm || typed.trim() === typeToConfirm;
  const run = async () => {
    if (!matches || !extraValid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      setBusy(false);
      onClose();
    } catch (err) {
      setBusy(false);
      setError(errorFor ? errorFor(err) : err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <Dialog
      title={title}
      onClose={onClose}
      alert={destructive}
      busy={busy}
      describedBy={bodyId}
      initialFocus={typeToConfirm ? "input" : destructive ? "[data-cancel]" : "[data-confirm]"}
      footer={
        <>
          <Button data-cancel="" onClick={onClose} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button data-confirm="" variant={destructive ? "danger" : "primary"} onClick={run} busy={busy} busyLabel={busyLabel} disabled={!matches || !extraValid}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div id={bodyId} className="dialog-text">
        {body}
      </div>
      {extra}
      {typeToConfirm ? (
        <div className="field">
          <label htmlFor={typeId} className="field-label">
            Type <span className="mono">{typeToConfirm}</span> to confirm
          </label>
          <input
            id={typeId}
            className="input"
            value={typed}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setTyped(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void run();
              }
            }}
          />
        </div>
      ) : null}
      {error ? <Notice variant="error">{error}</Notice> : null}
    </Dialog>
  );
}
