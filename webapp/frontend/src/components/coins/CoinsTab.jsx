import { useEffect, useState } from "react";
import { Ban, RotateCcw, Plus, Radio, TrendingUp, Check } from "lucide-react";
import { apiGet, apiPost, apiDelete } from "../../lib/api";
import { Spinner, EmptyRow } from "../common";
import { fmtDate, fmtPrice, fmtVolume } from "../../lib/format";
import { t } from "../../i18n";

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
  // Менять монеты может только администратор, остальные только смотрят
  const [canEditPool, setCanEditPool] = useState(false);
  const [candidatesError, setCandidatesError] = useState(null);
  const [busyCandidate, setBusyCandidate] = useState(null);
  const [showAllCandidates, setShowAllCandidates] = useState(false);

  const load = () => {
    apiGet("/symbols")
      .then((data) => { setSymbols(data.symbols); setCanEditPool(!!data.can_edit_pool); })
      .catch((e) => setError(e.message));
  };

  const loadCandidates = () => {
    apiGet("/symbols/candidates?limit=50")
      .then((data) => { setCandidates(data.candidates); setCanEditPool(!!data.can_edit_pool); })
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
          <span className="section-title">{t("top_coins")}</span>
          {candidates && <span className="section-count">{candidates.length}</span>}
        </div>
        <div className="row-sub row-sub-faint" style={{ padding: "0 14px 8px" }}>
          {t("top_coins_hint")}
        </div>

        {candidatesError && <div className="error-banner">{candidatesError}</div>}

        {!candidates && !candidatesError && (
          <div className="hero-loading"><Spinner /></div>
        )}

        {candidates && candidates.length === 0 && (
          <div className="list"><EmptyRow text={t("no_coins_filters")} /></div>
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
                      {c.held && <span className="tag tag-info">{t("in_position")}</span>}
                      {c.whitelisted && <span className="tag tag-success">{t("added_manually")}</span>}
                    </div>
                    <div className="row-sub">
                      {fmtPrice(c.price)}
                      {"  ·  "}
                      <span className={changeClass}>{changeSign}{c.change_pct.toFixed(1)}%</span>
                      {"  ·  "}
                      {t("vol24", { v: fmtVolume(c.volume_24h) })}
                    </div>
                  </div>
                  {inTrading ? (
                    c.whitelisted && canEditPool ? (
                      <button
                        className="icon-btn"
                        disabled={busyCandidate === c.symbol}
                        onClick={() => removeFromWhitelist(c.symbol)}
                        title={t("remove_whitelist")}
                      >
                        <RotateCcw size={15} />
                      </button>
                    ) : (
                      <div className="icon-btn icon-btn-success" title={t("already_trading")}>
                        <Check size={15} />
                      </div>
                    )
                  ) : canEditPool ? (
                    <button
                      className="icon-btn icon-btn-success"
                      disabled={busyCandidate === c.symbol}
                      onClick={() => addToTrading(c.symbol)}
                      title={t("add_to_trading")}
                    >
                      <TrendingUp size={15} />
                    </button>
                  ) : null}
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
            {showAllCandidates ? t("collapse") : t("show_n_more", { n: candidates.length - 10 })}
          </button>
        )}
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">{t("bot_coins")}</span>
          {symbols && <span className="section-count">{subscribedCount}</span>}
        </div>

        {!symbols && !error && (
          <div className="hero-loading"><Spinner /></div>
        )}

        {symbols && !activeSymbols.length && (
          <div className="list"><EmptyRow text={t("bot_no_coins")} /></div>
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
                    {s.held && <span className="tag tag-info">{t("in_position")}</span>}
                  </div>
                  <div className="row-sub">
                    {t("last_signal", { v: s.last_signal_at ? fmtDate(s.last_signal_at) : "—" })}
                    {"  ·  "}
                    {t("last_trade", { v: s.last_traded_at ? fmtDate(s.last_traded_at) : "—" })}
                  </div>
                </div>
                {canEditPool && (
                  <button
                    className="icon-btn icon-btn-danger"
                    disabled={busySymbol === s.symbol}
                    onClick={() => blacklistSymbol(s.symbol)}
                    title={t("add_blacklist")}
                  >
                    <Ban size={15} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {(canEditPool || blacklistedSymbols.length > 0) && <div className="section">
        <div className="section-head">
          <span className="section-title">{canEditPool ? t("add_blacklist") : t("blacklist")}</span>
        </div>
        {canEditPool && (
          <>
            <form className="list" onSubmit={addManually}>
              <div className="row row-compact">
                <input
                  className="param-input"
                  placeholder={t("blacklist_placeholder")}
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
              {t("blacklist_hint")}
            </div>
          </>
        )}

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
                    {s.held && <span className="tag tag-info">{t("in_position")}</span>}
                  </div>
                  <div className="row-sub">
                    {t("last_signal", { v: s.last_signal_at ? fmtDate(s.last_signal_at) : "—" })}
                    {"  ·  "}
                    {t("last_trade", { v: s.last_traded_at ? fmtDate(s.last_traded_at) : "—" })}
                  </div>
                </div>
                {canEditPool && (
                  <button
                    className="icon-btn"
                    disabled={busySymbol === s.symbol}
                    onClick={() => unblacklistSymbol(s.symbol)}
                    title={t("remove_blacklist")}
                  >
                    <RotateCcw size={15} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>}
    </div>
  );
}