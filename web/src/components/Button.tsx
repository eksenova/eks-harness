import { Link } from "@tanstack/react-router";
import { forwardRef, useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "md" | "sm";
  busy?: boolean;
  busyLabel?: string;
  icon?: IconName;
  block?: boolean;
}

function classes(variant: ButtonVariant, size: "md" | "sm", block?: boolean, extra?: string): string {
  return ["btn", variant !== "secondary" ? `btn--${variant}` : "", size === "sm" ? "btn--sm" : "", block ? "btn--block" : "", extra ?? ""].filter(Boolean).join(" ");
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", busy = false, busyLabel, icon, block, className, children, disabled, type = "button", style, ...rest },
  ref,
) {
  const inner = useRef<HTMLButtonElement | null>(null);
  const [width, setWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!busy && inner.current) setWidth(inner.current.offsetWidth);
  }, [busy, children]);
  return (
    <button
      {...rest}
      ref={(node) => {
        inner.current = node;
        if (typeof ref === "function") ref(node);
        else if (ref) ref.current = node;
      }}
      type={type}
      className={classes(variant, size, block, className)}
      disabled={disabled}
      aria-busy={busy || undefined}
      data-busy={busy || undefined}
      style={busy && width ? { ...style, minWidth: width } : style}
      onClick={busy ? undefined : rest.onClick}
    >
      {icon && !busy ? <Icon name={icon} /> : null}
      {busy && busyLabel ? busyLabel : children}
    </button>
  );
});

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: IconName;
  label: string;
  shortcut?: string;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({ icon, label, shortcut, className, type = "button", ...rest }, ref) {
  return (
    <button {...rest} ref={ref} type={type} className={`icon-btn ${className ?? ""}`} aria-label={label} title={shortcut ? `${label} (${shortcut})` : label}>
      <Icon name={icon} />
    </button>
  );
});

export function isExternal(to: string): boolean {
  return /^https?:/.test(to) || /^\/(raw|site|api|s|d|t|thumb)\//.test(to);
}

export function TextLink({ to, children, className, newTab, title, quiet }: { to: string; children: ReactNode; className?: string; newTab?: boolean; title?: string; quiet?: boolean }) {
  const cls = `${quiet ? "link-quiet" : "link"} ${className ?? ""}`;
  if (newTab || isExternal(to)) {
    return (
      <a href={to} className={cls} target={newTab ? "_blank" : undefined} rel={newTab ? "noopener noreferrer" : undefined} title={title}>
        {children}
      </a>
    );
  }
  return (
    <Link to={to} className={cls} title={title}>
      {children}
    </Link>
  );
}

export function ButtonLink({ to, children, variant = "secondary", size = "md", icon, download, newTab, className }: { to: string; children: ReactNode; variant?: ButtonVariant; size?: "md" | "sm"; icon?: IconName; download?: boolean; newTab?: boolean; className?: string }) {
  const cls = classes(variant, size, false, className);
  if (download || newTab || isExternal(to)) {
    return (
      <a href={to} className={cls} download={download ? "" : undefined} target={newTab ? "_blank" : undefined} rel={newTab ? "noopener noreferrer" : undefined}>
        {icon ? <Icon name={icon} /> : null}
        {children}
      </a>
    );
  }
  return (
    <Link to={to} className={cls}>
      {icon ? <Icon name={icon} /> : null}
      {children}
    </Link>
  );
}

export interface SegOption<T extends string> {
  value: T;
  label: string;
  icon?: IconName;
  title?: string;
}

export function Segmented<T extends string>({ options, value, onChange, label }: { options: SegOption<T>[]; value: T; onChange: (value: T) => void; label: string }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={`seg-item ${option.icon ? "seg-item--icon" : ""}`}
          aria-pressed={value === option.value}
          aria-label={option.icon ? option.label : undefined}
          title={option.title ?? (option.icon ? option.label : undefined)}
          onClick={() => onChange(option.value)}
        >
          {option.icon ? <Icon name={option.icon} /> : option.label}
        </button>
      ))}
    </div>
  );
}
