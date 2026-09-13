import { useState, useEffect } from "react";
import { Bell, Power, KeyRound, ShieldAlert } from "lucide-react";
import { apiGet, apiPost } from "../../lib/api";
import { Spinner, EmptyRow, ToggleRow, LinkRow } from "../common";
import StrategyCard from "./StrategyCard";

export default function SettingsTab() {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  const [tradingBusy, setTradingBusy] = useState(false);
  const [modeBusy, setModeBusy] = useState(false);
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

  const switchExchangeMode = async (goLive) => {
    // Переключение на LIVE — реальные деньги, поэтому отдельное явное
    // подтверждение, а не обычный клик по свитчу. window.confirm — самое
    // простое, что работает и в Telegram WebView, без отдельного модального
    // компонента, которого в проекте пока нет.
    const question = goLive
      ? "Переключить бота на LIVE? Начнёт торговать РЕАЛЬНЫМИ деньгами после перезапуска."
      : "Переключить бота на тестовый режим (testnet) после перезапуска?";
    if (!window.confirm(question)) return;

    setModeBusy(true);
    setError(null);
    try {
      const res = await apiPost("/settings/exchange-mode", { testnet: !goLive, confirm: true });
      setSettings((prev) => (prev ? {
        ...prev,
        exchange_testnet: res.exchange_testnet,
        pending_exchange_testnet: res.pending_exchange_testnet,
      } : prev));
    } catch (e) {
      setError(e.message);
    } finally {
      setModeBusy(false);
    }
  };

  const updateStrategy = (updated) => {
    setSettings((prev) => prev && {
      ...prev,
      strategies: prev.strategies.map((s) => (s.name === updated.name ? updated : s)),
    });
  };

  const liveTestnet = settings?.exchange_testnet;
  const pendingTestnet = settings?.pending_exchange_testnet;
  const restartPending = settings && pendingTestnet !== null && pendingTestnet !== undefined && pendingTestnet !== liveTestnet;
  const isLiveNow = liveTestnet === false;

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">{error}</div>}

      <div className="section">
        <div className="section-head">
          <span className="section-title">Режим биржи</span>
        </div>
        <div className="list">
          <ToggleRow
            icon={ShieldAlert}
            title={isLiveNow ? "🔴 LIVE — реальные деньги" : "🧪 Тестовый режим (testnet)"}
            sub={
              restartPending
                ? `Выбран ${pendingTestnet ? "тестовый" : "LIVE"} режим — применится после перезапуска бота (docker restart / redeploy).`
                : "Переключение требует, чтобы не было открытых позиций, и вступает в силу после перезапуска бота."
            }
            checked={!isLiveNow}
            disabled={!settings || modeBusy}
            onToggle={(wantsTestnet) => switchExchangeMode(!wantsTestnet)}
          />
        </div>
      </div>

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