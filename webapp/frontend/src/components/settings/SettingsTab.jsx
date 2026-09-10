import { useState, useEffect } from "react";
import { Bell, Power, KeyRound } from "lucide-react";
import { apiGet, apiPost } from "../../lib/api";
import { Spinner, EmptyRow, ToggleRow, LinkRow } from "../common";
import StrategyCard from "./StrategyCard";

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
          <span className="section-title">Торговля</span>
        </div>
        <div className="list">
          <ToggleRow
            icon={Power}
            title={settings?.trading_enabled ? "Торговля включена" : "Торговля выключена"}
            sub="Глобальный выключатель: если выключено, стратегии не открывают новые позиции. Уже открытые позиции продолжают сопровождаться (SL/TP)."
            checked={settings ? settings.trading_enabled : false}
            disabled={!settings || tradingBusy}
            onToggle={toggleTrading}
          />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Стратегии</span>
          {settings && <span className="section-count">{settings.strategies.length}</span>}
        </div>
        {!settings && !error && (
          <div className="hero-loading"><Spinner /></div>
        )}
        {settings && !settings.strategies.length && (
          <div className="list"><EmptyRow text="Стратегии ещё не инициализированы ботом" /></div>
        )}
        {settings?.strategies.map((s) => (
          <StrategyCard key={s.name} strategy={s} onUpdate={updateStrategy} />
        ))}
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Уведомления</span>
        </div>
        <div className="list">
          <ToggleRow icon={Bell} title="Открытие / закрытие позиций" defaultOn />
          <ToggleRow icon={Bell} title="Срабатывание SL / TP" defaultOn />
          <ToggleRow icon={Bell} title="Ошибки и сбои" defaultOn />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Доступ</span>
        </div>
        <div className="list">
          <LinkRow
            icon={KeyRound}
            title="API-ключ BingX"
            value={apiKeySuffix ? `•••• ${apiKeySuffix}` : "—"}
          />
        </div>
      </div>
    </div>
  );
}
