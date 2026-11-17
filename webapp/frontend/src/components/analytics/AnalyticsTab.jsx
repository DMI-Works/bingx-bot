import { useState, useEffect, useMemo } from "react";
import { ChevronDown, ChevronUp, Scissors, Target, SlidersHorizontal } from "lucide-react";
import { apiGet } from "../../lib/api";
import { fmtUsd, fmtPct, fmtDate, fmtTime, fmtDuration } from "../../lib/format";
import { Spinner, EmptyRow, PnlTag, SideBadge } from "../common";
import { t, tn } from "../../i18n";

const TRADES_PAGE_SIZE = 30;

// label — ключ перевода
const STATUS_FILTERS = [
  { id: "", label: "filter_all" },
  { id: "OPEN", label: "filter_open" },
  { id: "CLOSED", label: "filter_closed" },
];

// trade_analytics.strategy_params — произвольный снимок параметров стратегии
// на момент входа (разный набор ключей у разных стратегий), поэтому рендерим
// его как есть, а не через жёсткую схему полей.
function renderParamValue(v) {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return t(v ? "yes" : "no");
  return String(v);
}

function AnalyticsTradeRow({ trade, expanded, onToggle, detail, detailLoading }) {
  const tr = trade;
  const slCount = tr.sl_moves?.length ?? 0;
  const tpCount = tr.tp_fills?.length ?? 0;

  return (
    <div>
      <div className="row row-clickable" onClick={onToggle}>
        <div className="row-main">
          <div className="row-title-line">
            <span className="symbol">{tr.symbol}</span>
            <SideBadge side={tr.side} />
            {tr.status === "OPEN" && <span className="tag tag-info">{t("tag_open")}</span>}
            {tr.mode === "testnet" && <span className="tag">testnet</span>}
          </div>
          <div className="row-sub">
            {tr.strategy || t("manual")} · {fmtDate(tr.opened_at)}
            {slCount > 0 && <> · <Scissors size={10} style={{ verticalAlign: -1 }} /> {slCount}</>}
            {tpCount > 0 && <> · <Target size={10} style={{ verticalAlign: -1 }} /> {tpCount}</>}
          </div>
        </div>
        <div className="row-end">
          {tr.net_pnl != null && (
            <span className={`row-usd ${tr.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(tr.net_pnl)}</span>
          )}
          {tr.roe_percent != null && <PnlTag value={tr.net_pnl} pct={tr.roe_percent} />}
        </div>
        {expanded ? <ChevronUp size={16} className="chev" /> : <ChevronDown size={16} className="chev" />}
      </div>

      {expanded && (
        <div className="analytics-detail">
          {detailLoading && <div className="hero-loading"><Spinner /></div>}

          {detail && (
            <>
              <div className="kv-row">
                <span className="kv-label">{t("a_entry")}</span>
                <span className="kv-value">
                  {detail.entry_price?.toLocaleString()} · {detail.quantity} · {detail.leverage ?? "—"}x
                </span>
              </div>
              {detail.close_price != null && (
                <div className="kv-row">
                  <span className="kv-label">{t("a_exit")}</span>
                  <span className="kv-value">
                    {detail.close_price.toLocaleString()}
                    {detail.close_reason && <span className="row-sub-faint"> · {detail.close_reason}</span>}
                  </span>
                </div>
              )}
              {detail.duration_seconds != null && (
                <div className="kv-row">
                  <span className="kv-label">{t("a_duration")}</span>
                  <span className="kv-value">{fmtDuration(detail.duration_seconds)}</span>
                </div>
              )}
              {detail.commission_usdt != null && (
                <div className="kv-row">
                  <span className="kv-label">{t("a_commission")}</span>
                  <span className="kv-value">{fmtUsd(-Math.abs(detail.commission_usdt))}</span>
                </div>
              )}
              {detail.funding_fee != null && detail.funding_fee !== 0 && (
                <div className="kv-row">
                  <span className="kv-label">{t("funding")}</span>
                  <span className={`kv-value ${detail.funding_fee >= 0 ? "text-profit" : "text-loss"}`}>
                    {fmtUsd(detail.funding_fee)}
                  </span>
                </div>
              )}

              {detail.sl_moves?.length > 0 && (
                <div className="analytics-timeline">
                  <div className="analytics-timeline-title">{t("sl_moves_title")}</div>
                  {detail.sl_moves.map((m, i) => (
                    <div className="analytics-timeline-item" key={i}>
                      <span className="analytics-timeline-time">{fmtTime(m.at)}</span>
                      <span className="analytics-timeline-main">
                        {m.old_stop_price != null ? m.old_stop_price.toLocaleString() : "—"}
                        {" → "}
                        {m.new_stop_price?.toLocaleString()}
                        {m.reason === "fallback" && <span className="tag tag-danger" style={{ marginLeft: 6 }}>fallback</span>}
                      </span>
                      {m.roi_percent != null && (
                        <span className={m.roi_percent >= 0 ? "text-profit" : "text-loss"}>{fmtPct(m.roi_percent)}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {detail.tp_fills?.length > 0 && (
                <div className="analytics-timeline">
                  <div className="analytics-timeline-title">{t("tp_fills_title")}</div>
                  {detail.tp_fills.map((f, i) => (
                    <div className="analytics-timeline-item" key={i}>
                      <span className="analytics-timeline-time">{fmtTime(f.at)}</span>
                      <span className="analytics-timeline-main">{f.price?.toLocaleString()} × {f.quantity}</span>
                      <span className="text-profit">{fmtUsd(f.realized_pnl)}</span>
                    </div>
                  ))}
                </div>
              )}

              {detail.strategy_params && Object.keys(detail.strategy_params).length > 0 && (
                <div className="analytics-timeline">
                  <div className="analytics-timeline-title">{t("params_title")}</div>
                  {Object.entries(detail.strategy_params).map(([k, v]) => (
                    <div className="kv-row" key={k}>
                      <span className="kv-label">{k}</span>
                      <span className="kv-value">{renderParamValue(v)}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function AnalyticsTab() {
  const [summary, setSummary] = useState(null);
  const [trades, setTrades] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [status, setStatus] = useState("");
  const [strategyFilter, setStrategyFilter] = useState("");
  const [expandedId, setExpandedId] = useState(null);
  const [detailCache, setDetailCache] = useState({});
  const [detailLoadingId, setDetailLoadingId] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/analytics/summary")
      .then((d) => !cancelled && setSummary(d.summary))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setTrades(null);
    const params = new URLSearchParams({ limit: String(TRADES_PAGE_SIZE), offset: "0" });
    if (status) params.set("status", status);
    if (strategyFilter) params.set("strategy", strategyFilter);
    apiGet(`/analytics/trades?${params}`)
      .then((d) => {
        if (cancelled) return;
        setTrades(d.trades);
        setHasMore(d.trades.length === TRADES_PAGE_SIZE);
      })
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, [status, strategyFilter]);

  const loadMore = () => {
    if (loadingMore || !trades) return;
    setLoadingMore(true);
    const params = new URLSearchParams({ limit: String(TRADES_PAGE_SIZE), offset: String(trades.length) });
    if (status) params.set("status", status);
    if (strategyFilter) params.set("strategy", strategyFilter);
    apiGet(`/analytics/trades?${params}`)
      .then((d) => {
        setTrades((prev) => [...(prev || []), ...d.trades]);
        setHasMore(d.trades.length === TRADES_PAGE_SIZE);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoadingMore(false));
  };

  // Список стратегий для фильтра — из уже загруженной сводки, без
  // отдельного запроса специально за списком имён.
  const strategies = useMemo(() => {
    if (!summary) return [];
    return [...new Set(summary.map((s) => s.strategy).filter(Boolean))];
  }, [summary]);

  const toggleExpand = (orderId) => {
    if (expandedId === orderId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(orderId);
    if (!detailCache[orderId]) {
      setDetailLoadingId(orderId);
      apiGet(`/analytics/trades/${orderId}`)
        .then((d) => setDetailCache((prev) => ({ ...prev, [orderId]: d })))
        .catch((e) => setError(e.message))
        .finally(() => setDetailLoadingId(null));
    }
  };

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">{t("err_load_short", { msg: error })}</div>}

      {/* Сводка по стратегии+монете: отсортировано бэкендом по total_net_pnl
          по возрастанию — то, что сливает больше всего, сразу наверху. */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">{t("summary_title")}</span>
          {summary && <span className="section-count">{summary.length}</span>}
        </div>
        <div className="list">
          {!summary ? (
            <EmptyRow text={t("loading")} />
          ) : summary.length === 0 ? (
            <EmptyRow text={t("no_closed_for_analysis")} />
          ) : (
            summary.map((s) => (
              <div className="row" key={`${s.strategy}-${s.symbol}`}>
                <div className="row-main">
                  <div className="row-title-line">
                    <span className="symbol">{s.symbol}</span>
                    <span className="tag tag-info">{s.strategy || t("manual")}</span>
                  </div>
                  <div className="row-sub">
                    {tn("trades_count", s.trades)} · {s.wins}W / {s.losses}L
                    {s.avg_sl_moves > 0.05 && <> · {t("avg_sl", { v: s.avg_sl_moves.toFixed(1) })}</>}
                    {s.avg_duration_seconds != null && <> · {t("avg_value", { v: fmtDuration(s.avg_duration_seconds) })}</>}
                  </div>
                </div>
                <div className="row-end">
                  <span className={`row-usd ${s.total_net_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                    {fmtUsd(s.total_net_pnl)}
                  </span>
                  {s.avg_roe_percent != null && (
                    <span className="row-value">{t("avg_roe", { v: fmtPct(s.avg_roe_percent) })}</span>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Фильтры + детальная история */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">{t("trade_history")}</span>
        </div>

        <div className="chip-row analytics-filter-row">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.id}
              className={`pill ${status === f.id ? "pill-active" : ""}`}
              onClick={() => setStatus(f.id)}
            >
              {t(f.label)}
            </button>
          ))}
        </div>

        {strategies.length > 1 && (
          <div className="chip-row analytics-filter-row analytics-filter-row-wrap">
            <SlidersHorizontal size={13} className="row-sub-faint" style={{ marginRight: 2, alignSelf: "center" }} />
            <button
              className={`pill ${!strategyFilter ? "pill-active" : ""}`}
              onClick={() => setStrategyFilter("")}
            >
              {t("all_strategies")}
            </button>
            {strategies.map((s) => (
              <button
                key={s}
                className={`pill ${strategyFilter === s ? "pill-active" : ""}`}
                onClick={() => setStrategyFilter(s)}
              >
                {s}
              </button>
            ))}
          </div>
        )}

        <div className="list">
          {!trades ? (
            <EmptyRow text={t("loading")} />
          ) : trades.length === 0 ? (
            <EmptyRow text={t("no_trades_found")} />
          ) : (
            trades.map((tr) => (
              <AnalyticsTradeRow
                key={tr.order_id}
                trade={tr}
                expanded={expandedId === tr.order_id}
                onToggle={() => toggleExpand(tr.order_id)}
                detail={detailCache[tr.order_id]}
                detailLoading={detailLoadingId === tr.order_id}
              />
            ))
          )}
        </div>

        {hasMore && (
          <button className="load-more-btn" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? t("loading") : t("show_more")}
          </button>
        )}
      </div>
    </div>
  );
}