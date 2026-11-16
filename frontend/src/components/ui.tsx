import { useState, type ReactNode } from "react";
import type { Tone } from "../lib/format";

export function Panel({
  title,
  hint,
  className = "",
  children,
}: {
  title: string;
  hint?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`panel ${className}`} aria-label={title}>
      <header className="panel-head">
        <h2 className="panel-title">{title}</h2>
        {hint && <span className="hint">{hint}</span>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Chip({
  tone,
  icon,
  live,
  children,
}: {
  tone: Tone;
  icon?: string;
  live?: boolean;
  children: ReactNode;
}) {
  return (
    <span className="chip" data-tone={tone} data-live={live ? "true" : undefined}>
      {!live && icon && <span aria-hidden="true">{icon}</span>}
      {children}
    </span>
  );
}

export function CopyButton({ value, label = "copiar" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="copy"
      aria-label={`Copiar ${value}`}
      onClick={(e) => {
        e.stopPropagation();
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? "copiado ✓" : label}
    </button>
  );
}
