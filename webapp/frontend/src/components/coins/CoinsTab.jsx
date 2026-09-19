import { useEffect, useState } from "react";
import { Ban, RotateCcw, Plus, Radio } from "lucide-react";
import { apiGet, apiPost, apiDelete } from "../../lib/api";
import { Spinner, EmptyRow } from "../common";
import { fmtDate } from "../../lib/format";

export default function CoinsTab() {
  const [symbols, setSymbols] = useState(null);
  const [error, setError] = useState(null);
  const [busySymbol, setBusySymbol] = useState(null);
  const [newSymbol, setNewSymbol] = useState("");
  const [addBusy, setAddBusy] = useState(false);

  const load = () => {
    apiGet("/symbols")
      .then((data) => setSymbols(data.symbols))
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load();
    // Список подписок и время последнего сигнала меняются в фоне (ротация
    // раз в час, сигналы — чаще) — обновляем не только по действию юзера.
    const interval = setInterval(load, 30_000);
    return () => clearInterval(interval);
  }, []);

  const blacklistSymbol = async (symbol) => {
    setBusySymbol(symbol);
    setError(null);
    try {
      await apiPost("/symbols/blacklist", { symbol });
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusySymbol(null);
    }
  };

  const unblacklistSymbol = async (symbol) => {
    setBusySymbol(symbol);
    setError(null);
    try {
      await apiDelete(`/symbols/blacklist/${encodeURIComponent(symbol)}`);
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusySymbol(null);
    }
  };

  const addManually = async (e) => {
    e.preventDefault();
    const symbol = newSymbol.trim().toUpperCase();
    if (!symbol) return;
    setAddBusy(true);
    setError(null);
    try {
      await apiPost("/symbols/blacklist", { symbol });
      setNewSymbol("");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setAddBusy(false);
    }
  };

  const subscribedCount = symbols?.filter((s) => !s.blacklisted).length ?? 0;
  const activeSymbols = symbols?.filter((s) => !s.blacklisted) ?? [];
  const blacklistedSymbols = symbols?.filter((s) => s.blacklisted) ?? [];

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">{error}</div>}

      <div className="section">
        <div className="section-head">
          <span className="section-title">Монеты бота</span>
          {symbols && <span className="section-count">{subscribedCount}</span>}
        </div>

        {!symbols && !error && (
          <div className="hero-loading"><Spinner /></div>
        )}

        {symbols && !activeSymbols.length && (
          <div className="list"><EmptyRow text="Бот пока ни на одну монету не подписан" /></div>
        )}

        {activeSymbols.length > 0 && (
          <div className="list">
            {activeSymbols.map((s) => (
              <div className="row" key={s.symbol}>
                <div className="row-icon">
                  <Radio size={17} />
                </div>
                <div className="row-main">
                  <div className="row-title-line">
                    <span className="symbol">{s.symbol}</span>
                    {s.held && <span className="tag tag-info">в позиции</span>}
                  </div>
                  <div className="row-sub">
                    Сигнал: {s.last_signal_at ? fmtDate(s.last_signal_at) : "—"}
                    {"  ·  "}
                    Сделка: {s.last_traded_at ? fmtDate(s.last_traded_at) : "—"}
                  </div>
                </div>
                <button
                  className="icon-btn icon-btn-danger"
                  disabled={busySymbol === s.symbol}
                  onClick={() => blacklistSymbol(s.symbol)}
                  title="Добавить в чёрный список"
                >
                  <Ban size={15} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Добавить в чёрный список</span>
        </div>
        <form className="list" onSubmit={addManually}>
          <div className="row row-compact">
            <input
              className="param-input"
              placeholder="Например BTC-USDT"
              value={newSymbol}
              onChange={(e) => setNewSymbol(e.target.value)}
              disabled={addBusy}
            />
            <button className="param-save-btn" type="submit" disabled={addBusy || !newSymbol.trim()}>
              <Plus size={14} />
            </button>
          </div>
        </form>
        <div className="row-sub row-sub-faint" style={{ padding: "0 14px" }}>
          Монета не будет выбираться ботом при ротации, даже если сейчас на неё не подписаны.
          Уже открытые позиции при этом продолжают сопровождаться как обычно.
        </div>

        {blacklistedSymbols.length > 0 && (
          <div className="list" style={{ marginTop: 10 }}>
            {blacklistedSymbols.map((s) => (
              <div className="row" key={s.symbol}>
                <div className="row-icon">
                  <Ban size={17} />
                </div>
                <div className="row-main">
                  <div className="row-title-line">
                    <span className="symbol">{s.symbol}</span>
                    {s.held && <span className="tag tag-info">в позиции</span>}
                  </div>
                  <div className="row-sub">
                    Сигнал: {s.last_signal_at ? fmtDate(s.last_signal_at) : "—"}
                    {"  ·  "}
                    Сделка: {s.last_traded_at ? fmtDate(s.last_traded_at) : "—"}
                  </div>
                </div>
                <button
                  className="icon-btn"
                  disabled={busySymbol === s.symbol}
                  onClick={() => unblacklistSymbol(s.symbol)}
                  title="Убрать из чёрного списка"
                >
                  <RotateCcw size={15} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}