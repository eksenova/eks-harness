import { forwardRef, useId, useState, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { Icon } from "./Icon";

interface FieldProps {
  label: ReactNode;
  htmlFor?: string;
  helper?: ReactNode;
  error?: string | null;
  errorId?: string;
  aside?: ReactNode;
  sub?: ReactNode;
  children: ReactNode;
  className?: string;
  footer?: ReactNode;
}

export function Field({ label, htmlFor, helper, error, errorId, aside, sub, children, className, footer }: FieldProps) {
  return (
    <div className={`field ${className ?? ""}`}>
      <div className="field-label-row">
        <label htmlFor={htmlFor} className="field-label">
          {label}
        </label>
        {aside ? <span className="field-aside">{aside}</span> : null}
      </div>
      {sub}
      {children}
      {error ? (
        <p id={errorId} className="field-error">
          {error}
        </p>
      ) : helper ? (
        <p id={errorId} className="field-helper">
          {helper}
        </p>
      ) : null}
      {footer}
    </div>
  );
}

type InputProps = InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean; mono?: boolean };

export const TextInput = forwardRef<HTMLInputElement, InputProps>(function TextInput({ invalid, mono, className, ...rest }, ref) {
  return <input ref={ref} {...rest} aria-invalid={invalid || undefined} className={`input ${mono ? "mono" : ""} ${className ?? ""}`} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean; mono?: boolean }>(function Textarea(
  { invalid, mono, className, rows = 3, ...rest },
  ref,
) {
  return <textarea ref={ref} rows={rows} {...rest} aria-invalid={invalid || undefined} className={`input textarea ${mono ? "mono" : ""} ${className ?? ""}`} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean; wrapClassName?: string }>(function Select(
  { invalid, className, wrapClassName, children, ...rest },
  ref,
) {
  return (
    <span className={`select-wrap ${wrapClassName ?? ""}`}>
      <select ref={ref} {...rest} aria-invalid={invalid || undefined} className={`input select ${className ?? ""}`}>
        {children}
      </select>
      <Icon name="chevron-down" className="select-chevron" />
    </span>
  );
});

interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label?: ReactNode;
  indeterminate?: boolean;
  hiddenLabel?: string;
  helper?: ReactNode;
}

export function Checkbox({ label, indeterminate, hiddenLabel, helper, className, ...rest }: CheckboxProps) {
  const id = useId();
  return (
    <span className={`check-wrap ${className ?? ""}`}>
      <label className="check" htmlFor={rest.id ?? id}>
        <input
          {...rest}
          id={rest.id ?? id}
          type="checkbox"
          aria-label={hiddenLabel}
          ref={(node) => {
            if (node) node.indeterminate = Boolean(indeterminate);
          }}
        />
        <span className="check-box" aria-hidden="true">
          {indeterminate ? <span className="check-bar" /> : <Icon name="check" size={12} />}
        </span>
        {label ? <span className="check-label">{label}</span> : null}
      </label>
      {helper ? <span className="check-helper">{helper}</span> : null}
    </span>
  );
}

export function Radio({ label, helper, ...rest }: Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & { label: ReactNode; helper?: ReactNode }) {
  const id = useId();
  return (
    <span className="check-wrap">
      <label className="check" htmlFor={rest.id ?? id}>
        <input {...rest} id={rest.id ?? id} type="radio" />
        <span className="radio-mark" aria-hidden="true" />
        <span className="check-label">{label}</span>
      </label>
      {helper ? <span className="check-helper">{helper}</span> : null}
    </span>
  );
}

export const SearchInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { value: string; onValueChange: (value: string) => void; label: string; hideLabel?: boolean; hint?: ReactNode }>(
  function SearchInput({ value, onValueChange, label, hideLabel = true, hint, className, id, ...rest }, ref) {
    const autoId = useId();
    const inputId = id ?? autoId;
    return (
      <span className={`search-field ${className ?? ""}`}>
        <label htmlFor={inputId} className={hideLabel ? "visually-hidden" : "field-label"}>
          {label}
        </label>
        <input
          ref={ref}
          id={inputId}
          type="search"
          className="input"
          value={value}
          onChange={(event) => onValueChange(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          {...rest}
        />
        {value ? (
          <button type="button" className="icon-btn search-clear" aria-label={`Clear ${label.toLowerCase()}`} title="Clear" onClick={() => onValueChange("")}>
            <Icon name="close" />
          </button>
        ) : hint ? (
          <span className="search-hint" aria-hidden="true">
            {hint}
          </span>
        ) : null}
      </span>
    );
  },
);

export const PasswordInput = forwardRef<HTMLInputElement, InputProps>(function PasswordInput({ invalid, mono, className, ...rest }, ref) {
  const [shown, setShown] = useState(false);
  return (
    <span className="password-wrap">
      <input ref={ref} {...rest} type={shown ? "text" : "password"} aria-invalid={invalid || undefined} className={`input ${mono ? "mono" : ""} ${className ?? ""}`} />
      <button type="button" className="link password-toggle" onClick={() => setShown((v) => !v)} aria-pressed={shown}>
        {shown ? "Hide" : "Show"}
      </button>
    </span>
  );
});

export const NumberInput = forwardRef<HTMLInputElement, Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & { unit?: string; invalid?: boolean }>(function NumberInput(
  { unit, invalid, className, ...rest },
  ref,
) {
  return (
    <span className="number-wrap">
      <input ref={ref} {...rest} type="text" inputMode="numeric" aria-invalid={invalid || undefined} className={`input ${className ?? ""}`} />
      {unit ? <span className="number-unit">{unit}</span> : null}
    </span>
  );
});
