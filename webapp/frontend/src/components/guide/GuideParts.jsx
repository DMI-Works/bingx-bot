// Общие блоки инструкции; сам текст — в content/<lang>.jsx.
import { useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, Info } from "lucide-react";

export function Callout({ kind = "info", children }) {
  const Icon = kind === "warn" ? AlertTriangle : kind === "ok" ? CheckCircle2 : Info;
  return (
    <div className={`guide-callout guide-callout-${kind}`}>
      <Icon size={16} className="guide-callout-icon" />
      <div>{children}</div>
    </div>
  );
}

export function Step({ id, n, icon: Icon, title, children }) {
  return (
    <section className="guide-card" id={id}>
      <div className="guide-card-head">
        <div className="guide-step-badge">{n ?? <Icon size={18} />}</div>
        <div className="guide-card-title">
          {n != null && <Icon size={16} className="guide-card-title-icon" />}
          {title}
        </div>
      </div>
      <div className="guide-card-body">{children}</div>
    </section>
  );
}

export function Path({ items }) {
  return (
    <div className="guide-path">
      {items.map((x, i) => (
        <span key={x}>
          <span className="guide-path-item">{x}</span>
          {i < items.length - 1 && <span className="guide-path-sep">›</span>}
        </span>
      ))}
    </div>
  );
}

export function Faq({ q, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`guide-faq ${open ? "guide-faq-open" : ""}`}>
      <button className="guide-faq-q" onClick={() => setOpen(!open)}>
        <span>{q}</span>
        <ChevronDown size={16} className="guide-faq-chev" />
      </button>
      {open && <div className="guide-faq-a">{children}</div>}
    </div>
  );
}
