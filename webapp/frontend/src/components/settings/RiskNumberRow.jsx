import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { apiPost } from "../../lib/api";

// Числовая риск-настройка из /api/settings/risk (min_margin_usdt,
// max_stop_roi_percent). Бэкенд валидирует диапазон и возвращает все значения.
export default function RiskNumberRow({ field, title, sub, value, format, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  const openEditor = () => {
    setDraft(String(value ?? ""));
    setErr(null);
    setEditing(true);
  };

  const commit = async () => {
    const n = Number(draft);
    if (draft.trim() === "" || Number.isNaN(n) || n < 0) { setErr("Введите число ≥ 0"); return; }
    setSaving(true);
    setErr(null);
    try {
      const res = await apiPost("/settings/risk", { [field]: n });
      onSaved(res);
      setEditing(false);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    const ready = value !== undefined && value !== null;
    return (
      <div className="row" onClick={ready ? openEditor : undefined} style={{ cursor: "pointer" }}>
        <div className="row-main">
          <span className="settings-title">{title}</span>
          <div className="row-sub">{sub}</div>
        </div>
        <span className="row-value">{ready ? format(value) : "—"}</span>
        <ChevronRight size={16} className="chev" />
      </div>
    );
  }

  return (
    <div className="row">
      <div className="row-main">
        <span className="settings-title">{title}</span>
        <div className="param-edit-line">
          <input
            className="param-input"
            type="number"
            inputMode="decimal"
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
