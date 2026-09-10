import { useState } from "react";
import { ChevronRight, Power, RotateCcw } from "lucide-react";
import { apiPost } from "../../lib/api";
import { ToggleRow, EmptyRow } from "../common";
import ParamRow from "./ParamRow";

export default function StrategyCard({ strategy, onUpdate }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const toggleEnabled = async (next) => {
    setBusy(true);
    setErr(null);
    try {
      onUpdate(await apiPost(`/settings/strategies/${strategy.name}/enabled`, { enabled: next }));
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    setErr(null);
    try {
      onUpdate(await apiPost(`/settings/strategies/${strategy.name}/reset`, {}));
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="strategy-card">
      <div className="row" onClick={() => setOpen(!open)} style={{ cursor: "pointer", borderBottom: open ? undefined : "none" }}>
        <span className={`status-dot ${strategy.enabled ? "status-on" : "status-off"}`} />
        <div className="row-main">
          <div className="row-title-line">
            <span className="settings-title">{strategy.name}</span>
            {strategy.modified && <span className="modified-badge">изменено</span>}
          </div>
        </div>
        <ChevronRight size={16} className="chev" style={{ transform: open ? "rotate(90deg)" : "none" }} />
      </div>

      {open && (
        <div className="strategy-body">
          {err && <div className="error-banner">{err}</div>}
          <div className="list" style={{ marginBottom: 10 }}>
            <ToggleRow
              icon={Power}
              title="Стратегия включена"
              checked={strategy.enabled}
              disabled={busy}
              onToggle={toggleEnabled}
            />
          </div>
          <div className="list">
            {strategy.params.length
              ? strategy.params.map((p) => (
                  <ParamRow key={p.key} strategyName={strategy.name} param={p} onSaved={onUpdate} />
                ))
              : <EmptyRow text="Нет параметров" />}
          </div>
          <button className="danger-btn" onClick={reset} disabled={busy}>
            <RotateCcw size={16} />
            Сбросить к заводским
          </button>
        </div>
      )}
    </div>
  );
}
