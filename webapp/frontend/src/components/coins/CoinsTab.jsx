import { useEffect, useState } from "react";
import { Ban, RotateCcw, Plus, Radio, TrendingUp, Check } from "lucide-react";
import { apiGet, apiPost, apiDelete } from "../../lib/api";
import { Spinner, EmptyRow } from "../common";
import { fmtDate } from "../../lib/format";

function fmtVolume(v) {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(v);
}

function fmtPrice(p) {
  if (p == null) return "—";
  return p >= 1 ? p.toLocaleString("ru-RU", { maximumFractionDigits: 2 }) : p.toPrecision(3);
}

export default function CoinsTab() {
  const [symbols, setSymbols] = useState(null);
  const [error, setError] = useState(null);
  const [busySymbol, setBusySymbol] = useState(null);
  const [newSymbol, setNewSymbol] = useState("");
  const [addBusy, setAddBusy] = useState(false);

  // «Топ монет» — полный ранжированный список кандидатов, как их видит
  // SymbolSelector при отборе (core/exchange/symbol_selector.py), а не
  // только уже подписанные. Именно отсюда можно принудительно добавить
  // монету в торговлю (whitelist), даже если бот её сам ещё не выбрал.
  const [candidates, setCandidates] = useState(null);
  const [candidatesError, setCandidatesError] = useState(null);
  const [busyCandidate, setBusyCandidate] = useState(null);
  const [showAllCandidates, setShowAllCandidates] = useState(false);

  const load = () => {
    apiGet("/symbols")
      .then((data) => setSymbols(data.symbols))
      .catch((e) => setError(e.message));
  };

  const loadCandidates = () => {
    apiGet("/symbols/candidates?limit=50")
      .then((data) => setCandidates(data.candidates))
      .catch((e) => setCandidatesError(e.message));
  };

  useEffect(() => {
    load();
    loadCandidates();
    // Список подписок и время последнего сигнала меняются в фоне (ротация
    // раз в час, сигналы — чаще) — обновляем не только по действию юзера.
    const interval = setInterval(() => {
      load();
      loadCandidates();
    }, 30_000);
    return () => clearInterval(interval);
  }, []);

  const addToTrading = async (symbol) => {
    setBusyCandidate(symbol);
    setCandidatesError(null);
    try {
      await apiPost("/symbols/whitelist", { symbol });
      loadCandidates();
      load();
    } catch (e) {
      setCandidatesError(e.message);
    } finally {
      setBusyCandidate(null);
    }
  };

  const removeFromWhitelist = async (symbol) => {
    setBusyCandidate(symbol);
    setCandidatesError(null);
    try {
      await apiDelete(`/symbols/whitelist/${encodeURIComponent(symbol)}`);
      loadCandidates();
      load();
    } catch (e) {
      setCandidatesError(e.message);
    } finally {
      setBusyCandidate(null);
    }
  };

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

  const visibleCandidates = showAllCandidates ? candidates : candidates?.slice(0, 10);

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">{error}</div>}

      <div className="section">
        <div className="section-head">
          <span className="section-title">Топ монет</span>
          {candidates && <span className="section-count">{candidates.length}</span>}
        </div>
        <div className="row-sub row-sub-faint" style={{ padding: "0 14px 8px" }}>
          Так бот ранжирует монеты по объёму торгов при отборе (см. SymbolSelector).
          «В торговле» — уже подписан или принудительно добавлен отсюда.
        </div>

        {candidatesError && <div className="error-banner">{candidatesError}</div>}

        {!candidates && !candidatesError && (
          <div className="hero-loading"><Spinner /></div>
        )}

        {candidates && candidates.length === 0 && (
          <div className="list"><EmptyRow text="Нет монет, проходящих текущие фильтры" /></div>
        )}

        {visibleCandidates && visibleCandidates.length > 0 && (
          <div className="list">
            {visibleCandidates.map((c, i) => {
              const inTrading = c.subscribed || c.whitelisted;
              const changeClass = c.change_pct >= 0 ? "text-profit" : "text-loss";
              const changeSign = c.change_pct >= 0 ? "+" : "";
              return (
                <div className="row" key={c.symbol}>
                  <div className="row-icon">
                    <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{i + 1}</span>
                  </div>
                  <div className="row-main">
                    <div className="row-title-line">
                      <span className="symbol">{c.symbol}</span>
                      {c.held && <span className="tag tag-info">в позиции</span>}
                      {c.whitelisted && <span className="tag tag-success">добавлена вручную</span>}
                    </div>
                    <div className="row-sub">
                      {fmtPrice(c.price)}
                      {"  ·  "}
                      <span className={changeClass}>{changeSign}{c.change_pct.toFixed(1)}%</span>
                      {"  ·  "}
                      об. 24ч {fmtVolume(c.volume_24h)}
                    </div>
                  </div>
                  {inTrading ? (
                    c.whitelisted ? (
                      <button
                        className="icon-btn"
                        disabled={busyCandidate === c.symbol}
                        onClick={() => removeFromWhitelist(c.symbol)}
                        title="Убрать из принудительного списка"
                      >
                        <RotateCcw size={15} />
                      </button>
                    ) : (
                      <div className="icon-btn icon-btn-success" title="Уже в торговле (отобран автоматически)">
                        <Check size={15} />
                      </div>
                    )
                  ) : (
                    <button
                      className="icon-btn icon-btn-success"
                      disabled={busyCandidate === c.symbol}
                      onClick={() => addToTrading(c.symbol)}
                      title="Добавить в торговлю"
                    >
                      <TrendingUp size={15} />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {candidates && candidates.length > 10 && (
          <button
            className="param-save-btn"
            style={{ margin: "10px 14px 0" }}
            onClick={() => setShowAllCandidates((v) => !v)}
          >
            {showAllCandidates ? "Свернуть" : `Показать ещё ${candidates.length - 10}`}
          </button>
        )}
      </div>

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