import { useState, useEffect } from "react";
import { Power, KeyRound } from "lucide-react";
import { apiGet, apiPost } from "../../lib/api";
import { EmptyRow, ToggleRow, LinkRow } from "../common";
import StrategyCard from "./StrategyCard";
import RiskNumberRow from "./RiskNumberRow";
import { t } from "../../i18n";

export default function SettingsTab() {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  const [tradingBusy, setTradingBusy] = useState(false);
  // Реальный суффикс API-ключа берём из /api/profile (тот же источник, что
  // и вкладка "Профиль") — раньше здесь было захардкожено "•••• 84f2".
  const [apiKeySuffix, setApiKeySuffix] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/settings")
      .then((data) => !cancelled && setSettings(data))
      .catch((e) => !cancelled && setError(e.message));
    apiGet("/profile")
      .then((data) => !cancelled && setApiKeySuffix(data.api_key_suffix))
      .catch(() => { /* необязательные данные — не блокируем вкладку из-за них */ });
    return () => { cancelled = true; };
  }, []);

  const toggleTrading = async (next) => {
    setTradingBusy(true);
    setError(null);
    try {
      const res = await apiPost("/settings/trading", { enabled: next });
      setSettings((prev) => (prev ? { ...prev, trading_enabled: res.trading_enabled } : prev));
    } catch (e) {
      setError(e.message);
    } finally {
      setTradingBusy(false);
    }
  };

  const saveRisk = (risk) => setSettings((prev) => (prev ? { ...prev, ...risk } : prev));

  const updateStrategy = (updated) => {
    setSettings((prev) => prev && {
      ...prev,
      strategies: prev.strategies.map((s) => (s.name === updated.name ? updated : s)),
    });
  };

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">{error}</div>}

      <div className="section">
        <div className="section-head">
          <span className="section-title">{t("trading")}</span>
        </div>
        <div className="list">
          <ToggleRow
            icon={Power}
            title={t(settings?.trading_enabled ? "trading_on" : "trading_off")}
            sub={t("trading_sub")}
            checked={settings ? settings.trading_enabled : false}
            disabled={!settings || tradingBusy}
            onToggle={toggleTrading}
          />
          <RiskNumberRow
            field="max_open_positions"
            title={t("max_positions_title")}
            sub={t("max_positions_sub")}
            value={settings?.max_open_positions}
            format={(v) => String(v)}
            onSaved={saveRisk}
          />
          <RiskNumberRow
            field="min_margin_usdt"
            title={t("min_margin_title")}
            sub={t("min_margin_sub")}
            value={settings?.min_margin_usdt}
            format={(v) => `$${v}`}
            onSaved={saveRisk}
          />
          <RiskNumberRow
            field="max_margin_usdt"
            title={t("max_margin_title")}
            sub={t("max_margin_sub")}
            value={settings?.max_margin_usdt}
            format={(v) => (v > 0 ? `$${v}` : t("off"))}
            onSaved={saveRisk}
          />
          <RiskNumberRow
            field="max_stop_roi_percent"
            title={t("max_stop_title")}
            sub={t("max_stop_sub")}
            value={settings?.max_stop_roi_percent}
            format={(v) => `−${v}%`}
            onSaved={saveRisk}
          />
        </div>
      </div>

      {settings?.is_admin && (
        <div className="section">
          <div className="section-head">
            <span className="section-title">{t("strategies")}</span>
            <span className="section-count">{settings.strategies.length}</span>
          </div>
          {!settings.strategies.length && (
            <div className="list"><EmptyRow text={t("strategies_not_init")} /></div>
          )}
          {settings.strategies.map((s) => (
            <StrategyCard key={s.name} strategy={s} onUpdate={updateStrategy} />
          ))}
        </div>
      )}

      <div className="section">
        <div className="section-head">
          <span className="section-title">{t("access")}</span>
        </div>
        <div className="list">
          <LinkRow
            icon={KeyRound}
            title={t("api_key_bingx")}
            value={apiKeySuffix ? `•••• ${apiKeySuffix}` : "—"}
          />
        </div>
      </div>
    </div>
  );
}
