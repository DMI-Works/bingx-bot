import { useState, useEffect, useMemo } from "react";
import { ChevronDown, ChevronUp, Scissors, Target, SlidersHorizontal } from "lucide-react";
import { apiGet } from "../../lib/api";
import { fmtUsd, fmtPct, fmtDate, fmtTime, fmtDuration } from "../../lib/format";
import { Spinner, EmptyRow, PnlTag, SideBadge } from "../common";

const TRADES_PAGE_SIZE = 30;

const STATUS_FILTERS = [
  { id: "", label: "Все" },
  { id: "OPEN", label: "Открытые" },
  { id: "CLOSED", label: "Закрытые" },
];

// trade_analytics.strategy_params — произвольный снимок параметров стратегии
// на момент входа (разный набор ключей у разных стратегий), поэтому рендерим
// его как есть, а не через жёсткую схему полей.
function renderParamValue(v) {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return v ? "да" : "нет";
  return String(v);
}

function AnalyticsTradeRow({ trade, expanded, onToggle, detail, detailLoading }) {
  const t = trade;
  const slCount = t.sl_moves?.length ?? 0;
  const tpCount = t.tp_fills?.length ?? 0;

  return (
    <div>
      <div className="row row-clickable" onClick={onToggle}>
        <div className="row-main">
          <div className="row-title-line">
            <span className="symbol">{t.symbol}</span>
            <SideBadge side={t.side} />
            {t.status === "OPEN" && <span className="tag tag-info">открыта</span>}
            {t.mode === "testnet" && <span className="tag">testnet</span>}
          </div>
          <div className="row-sub">
            {t.strategy || "вручную"} · {fmtDate(t.opened_at)}
            {slCount > 0 && <> · <Scissors size={10} style={{ verticalAlign: -1 }} /> {slCount}</>}
            {tpCount > 0 && <> · <Target size={10} style={{ verticalAlign: -1 }} /> {tpCount}</>}
          </div>
        </div>
        <div className="row-end">
          {t.net_pnl != null && (
            <span className={`row-usd ${t.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(t.net_pnl)}</span>
          )}
          {t.roe_percent != null && <PnlTag value={t.net_pnl} pct={t.roe_percent} />}
        </div>
        {expanded ? <ChevronUp size={16} className="chev" /> : <ChevronDown size={16} className="chev" />}
      </div>

      {expanded && (
        <div className="analytics-detail">
          {detailLoading && <div className="hero-loading"><Spinner /></div>}

          {detail && (
            <>
              <div className="kv-row">
                <span className="kv-label">Вход</span>
                <span className="kv-value">
                  {detail.entry_price?.toLocaleString()} · {detail.quantity} · {detail.leverage ?? "—"}x
                </span>
              </div>
              {detail.close_price != null && (
                <div className="kv-row">
                  <span className="kv-label">Выход</span>
                  <span className="kv-value">
                    {detail.close_price.toLocaleString()}
                    {detail.close_reason && <span className="row-sub-faint"> · {detail.close_reason}</span>}
                  </span>
                </div>
              )}
              {detail.duration_seconds != null && (
                <div className="kv-row">
                  <span className="kv-label">Длительность</span>
                  <span className="kv-value">{fmtDuration(detail.duration_seconds)}</span>
                </div>
              )}
              {detail.commission_usdt != null && (
                <div className="kv-row">
                  <span className="kv-label">Комиссия</span>
                  <span className="kv-value">{fmtUsd(-Math.abs(detail.commission_usdt))}</span>
                </div>
              )}
              {detail.funding_fee != null && detail.funding_fee !== 0 && (
                <div className="kv-row">
                  <span className="kv-label">Фандинг</span>
                  <span className={`kv-value ${detail.funding_fee >= 0 ? "text-profit" : "text-loss"}`}>
                    {fmtUsd(detail.funding_fee)}
                  </span>
                </div>
              )}

              {detail.sl_moves?.length > 0 && (
                <div className="analytics-timeline">
                  <div className="analytics-timeline-title">Перестановки SL</div>
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
                  <div className="analytics-timeline-title">Срабатывания TP</div>
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
                  <div className="analytics-timeline-title">Параметры стратегии на входе</div>
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
      {error && <div className="error-banner">Не удалось загрузить: {error}</div>}

      {/* Сводка по стратегии+монете: отсортировано бэкендом по total_net_pnl
          по возрастанию — то, что сливает больше всего, сразу наверху. */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">Сводка по стратегиям</span>
          {summary && <span className="section-count">{summary.length}</span>}
        </div>
        <div className="list">
          {!summary ? (
            <EmptyRow text="Загрузка..." />
          ) : summary.length === 0 ? (
            <EmptyRow text="Пока нет закрытых сделок для анализа" />
          ) : (
            summary.map((s) => (
              <div className="row" key={`${s.strategy}-${s.symbol}`}>
                <div className="row-main">
                  <div className="row-title-line">
                    <span className="symbol">{s.symbol}</span>
                    <span className="tag tag-info">{s.strategy || "вручную"}</span>
                  </div>
                  <div className="row-sub">
                    {s.trades} сделок · {s.wins}W / {s.losses}L
                    {s.avg_sl_moves > 0.05 && <> · SL x{s.avg_sl_moves.toFixed(1)} в среднем</>}
                    {s.avg_duration_seconds != null && <> · {fmtDuration(s.avg_duration_seconds)} в среднем</>}
                  </div>
                </div>
                <div className="row-end">
                  <span className={`row-usd ${s.total_net_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                    {fmtUsd(s.total_net_pnl)}
                  </span>
                  {s.avg_roe_percent != null && (
                    <span className="row-value">{fmtPct(s.avg_roe_percent)} ср. ROE</span>
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
          <span className="section-title">История сделок</span>
        </div>

        <div className="chip-row analytics-filter-row">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.id}
              className={`pill ${status === f.id ? "pill-active" : ""}`}
              onClick={() => setStatus(f.id)}
            >
              {f.label}
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
              Все стратегии
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
            <EmptyRow text="Загрузка..." />
          ) : trades.length === 0 ? (
            <EmptyRow text="Сделок не найдено" />
          ) : (
            trades.map((t) => (
              <AnalyticsTradeRow
                key={t.order_id}
                trade={t}
                expanded={expandedId === t.order_id}
                onToggle={() => toggleExpand(t.order_id)}
                detail={detailCache[t.order_id]}
                detailLoading={detailLoadingId === t.order_id}
              />
            ))
          )}
        </div>

        {hasMore && (
          <button className="load-more-btn" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Загрузка..." : "Показать ещё"}
          </button>
        )}
      </div>
    </div>
  );
}