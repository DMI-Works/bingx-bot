import { useState } from "react";
import { ChevronRight, Sliders } from "lucide-react";
import { apiPost } from "../../lib/api";
import { ToggleRow } from "../common";

export default function ParamRow({ strategyName, param, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(param.value ?? ""));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  const openEditor = () => {
    setDraft(String(param.value ?? ""));
    setErr(null);
    setEditing(true);
  };

  const commit = async () => {
    let value = draft;
    if (param.kind === "number") {
      const n = Number(draft);
      if (Number.isNaN(n)) { setErr("Введите число"); return; }
      value = n;
    }
    setSaving(true);
    setErr(null);
    try {
      const updated = await apiPost(`/settings/strategies/${strategyName}/params`, { key: param.key, value });
      onSaved(updated);
      setEditing(false);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  };

  if (param.kind === "bool") {
    return (
      <ToggleRow
        icon={Sliders}
        title={param.label}
        sub={param.description}
        checked={!!param.value}
        disabled={saving}
        onToggle={async (next) => {
          setSaving(true);
          try {
            const updated = await apiPost(`/settings/strategies/${strategyName}/params`, { key: param.key, value: next });
            onSaved(updated);
          } catch (e) {
            setErr(e.message);
          } finally {
            setSaving(false);
          }
        }}
      />
    );
  }

  if (!editing) {
    return (
      <div className="row" onClick={openEditor} style={{ cursor: "pointer" }}>
        <div className="row-main">
          <span className="settings-title">{param.label}</span>
          {param.description && <div className="row-sub">{param.description}</div>}
        </div>
        <span className="row-value">{String(param.value)}</span>
        <ChevronRight size={16} className="chev" />
      </div>
    );
  }

  return (
    <div className="row">
      <div className="row-main">
        <span className="settings-title">{param.label}</span>
        <div className="param-edit-line">
          <input
            className="param-input"
            type={param.kind === "number" ? "number" : "text"}
            inputMode={param.kind === "number" ? "decimal" : "text"}
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <button className="param-save-btn" onClick={commit} disabled={saving}>
            {saving ? "…" : "OK"}
          </button>
        </div>
        {err && <div className="row-sub" style={{ color: "var(--loss)" }}>{err}</div>}
      </div>
    </div>
  );
}
