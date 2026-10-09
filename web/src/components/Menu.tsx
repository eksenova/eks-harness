import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

export interface MenuItem {
  label: string;
  onSelect?: () => void;
  danger?: boolean;
  disabled?: boolean;
  title?: string;
  kind?: "item" | "separator" | "text" | "radio";
  checked?: boolean;
}

export function separator(): MenuItem {
  return { label: "", kind: "separator" };
}

interface MenuProps {
  items: MenuItem[];
  label: string;
  trigger?: (props: { ref: React.Ref<HTMLButtonElement>; onClick: () => void; expanded: boolean }) => ReactNode;
  className?: string;
}

export function OverflowMenu({ items, label, trigger, className }: MenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const close = (focus = true) => {
    setOpen(false);
    if (focus) triggerRef.current?.focus();
  };
  return (
    <>
      {trigger ? (
        trigger({ ref: triggerRef, onClick: () => setOpen((v) => !v), expanded: open })
      ) : (
        <button
          ref={triggerRef}
          type="button"
          className={`icon-btn ${className ?? ""}`}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={label}
          title={label}
          onClick={(event) => {
            event.stopPropagation();
            setOpen((v) => !v);
          }}
        >
          <Icon name="more" />
        </button>
      )}
      {open ? <MenuPopup items={items} anchor={triggerRef.current} onClose={close} label={label} /> : null}
    </>
  );
}

function MenuPopup({ items, anchor, onClose, label }: { items: MenuItem[]; anchor: HTMLElement | null; onClose: (focus?: boolean) => void; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const actionable = items.map((item, index) => ({ item, index })).filter(({ item }) => (item.kind ?? "item") !== "separator" && item.kind !== "text" && !item.disabled);
  const [active, setActive] = useState(actionable[0]?.index ?? -1);

  useLayoutEffect(() => {
    if (!anchor || !ref.current) return;
    const rect = anchor.getBoundingClientRect();
    const menu = ref.current.getBoundingClientRect();
    let left = rect.right - menu.width;
    if (left < 8) left = Math.min(rect.left, window.innerWidth - menu.width - 8);
    let top = rect.bottom + 4;
    if (top + menu.height > window.innerHeight - 8) top = Math.max(8, rect.top - menu.height - 4);
    setPosition({ top, left: Math.max(8, left) });
  }, [anchor]);

  useEffect(() => {
    ref.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.focus();
  }, [active, position]);

  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (ref.current?.contains(event.target as Node) || anchor?.contains(event.target as Node)) return;
      onClose(false);
    };
    const onScroll = () => onClose(false);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", onScroll);
    };
  }, [anchor, onClose]);

  const move = (delta: number) => {
    if (!actionable.length) return;
    const current = actionable.findIndex(({ index }) => index === active);
    const next = (current + delta + actionable.length) % actionable.length;
    setActive(actionable[next].index);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    event.stopPropagation();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      setActive(actionable[0]?.index ?? -1);
    } else if (event.key === "End") {
      event.preventDefault();
      setActive(actionable[actionable.length - 1]?.index ?? -1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose(true);
    } else if (event.key === "Tab") {
      onClose(false);
    } else if (event.key.length === 1 && /\S/.test(event.key)) {
      const letter = event.key.toLowerCase();
      const start = actionable.findIndex(({ index }) => index === active);
      for (let i = 1; i <= actionable.length; i += 1) {
        const candidate = actionable[(start + i) % actionable.length];
        if (candidate.item.label.toLowerCase().startsWith(letter)) {
          setActive(candidate.index);
          break;
        }
      }
    }
  };

  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      className="menu"
      data-overlay-open=""
      style={position ? { top: position.top, left: position.left } : { top: -9999, left: -9999 }}
      onKeyDown={onKeyDown}
    >
      {items.map((item, index) => {
        const kind = item.kind ?? "item";
        if (kind === "separator") return <div key={index} role="separator" className="menu-sep" />;
        if (kind === "text") return <div key={index} className="menu-text">{item.label}</div>;
        return (
          <button
            key={index}
            type="button"
            data-index={index}
            role={kind === "radio" ? "menuitemradio" : "menuitem"}
            aria-checked={kind === "radio" ? Boolean(item.checked) : undefined}
            className={`menu-item ${item.danger ? "menu-danger" : ""}`}
            tabIndex={index === active ? 0 : -1}
            disabled={item.disabled}
            title={item.title}
            onMouseEnter={() => !item.disabled && setActive(index)}
            onClick={(event) => {
              event.stopPropagation();
              onClose(!item.onSelect);
              item.onSelect?.();
            }}
          >
            {item.label}
          </button>
        );
      })}
    </div>,
    document.body,
  );
}
