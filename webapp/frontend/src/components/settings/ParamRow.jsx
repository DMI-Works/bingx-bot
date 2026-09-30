import { useState } from "react";
import { ChevronRight, Sliders } from "lucide-react";
import { apiPost } from "../../lib/api";
import { ToggleRow } from "../common";

// Массивы редактируются одной строкой через запятую: "10, 16, 32, 64".
// item_kind приходит с бэкенда ("number" | "text" | "bool"), так что любой
// новый параметр-массив в DEFAULT_PARAMS стратегии заработает без правок UI.
const formatValue = (param) =>
  param.kind === "list" && Array.isArray(param.value)
    ? param.value.join(", ")
    : String(param.value ?? "");

const parseList = (text, itemKind) => {
  const parts = text.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
  if (itemKind === "text") return { value: parts };
  if (itemKind === "bool") {
    return { error: "Списки логических значений не поддерживаются" };
  }
  const nums = parts.map((x) => Number(x));
  if (nums.some((n) => Number.isNaN(n))) return { error: "Введите числа через запятую" };
  return { value: nums };
};

export default function ParamRow({ strategyName, param, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(formatValue(param));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  const openEditor = () => {
    setDraft(formatValue(param));
    setErr(null);
    setEditing(true);
  };

  const commit = async () => {
    let value = draft;
    if (param.kind === "number") {
      const n = Number(draft);
      if (Number.isNaN(n)) { setErr("Введите число"); return; }
      value = n;
    } else if (param.kind === "list") {
      const parsed = parseList(draft, param.item_kind);
      if (parsed.error) { setErr(parsed.error); return; }
      value = parsed.value;
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
        <span className="row-value" style={param.kind === "list" ? { maxWidth: "55%", wordBreak: "break-word", textAlign: "right" } : undefined}>
          {formatValue(param)}
        </span>
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
