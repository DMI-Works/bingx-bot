import { useState, useMemo, useEffect } from "react";
import {
  AreaChart, Area, XAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import { TrendingUp, TrendingDown } from "lucide-react";
import { apiGet } from "../../lib/api";
import { fmtUsd, fmtPct, slRoiPercent } from "../../lib/format";
import { Spinner, EmptyRow, PnlTag, SideBadge } from "../common";
import TradeHistoryList from "./TradeHistoryList";

const PERIODS = ["1D", "1W", "1M", "ALL"];

function StatRow({ label, hint, children }) {
  return (
    <div className="stat-row">
      <div className="stat-row-main">
        <span className="stat-label">{label}</span>
        {hint && <span className="stat-hint">{hint}</span>}
      </div>
      <div className="stat-value">{children}</div>
    </div>
  );
}

const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
};

export default function StatisticsTab() {
  const [period, setPeriod] = useState("1W");
  const [stats, setStats] = useState(null);
  const [positions, setPositions] = useState(null);
  const [trades, setTrades] = useState(null);
  const [tradesTotal, setTradesTotal] = useState(0);
  const [tradesLoadingMore, setTradesLoadingMore] = useState(false);
  const [error, setError] = useState(null);

  const TRADES_PAGE_SIZE = 50;

  useEffect(() => {
    let cancelled = false;
    setStats(null);
    apiGet(`/stats?period=${period}`)
      .then((data) => !cancelled && setStats(data))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, [period]);

  useEffect(() => {
    let cancelled = false;
    apiGet("/positions")
      .then((data) => !cancelled && setPositions(data))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setTrades(null);
    setTradesTotal(0);
   
    apiGet(`/trades?limit=${TRADES_PAGE_SIZE}&offset=0&period=${period}`)
      .then((data) => {
        if (cancelled) return;
        setTrades(data.trades);
        setTradesTotal(data.total ?? data.trades.length);
      })
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, [period]);

  const loadMoreTrades = () => {
    if (tradesLoadingMore || !trades) return;
    setTradesLoadingMore(true);
    const nextOffset = trades.length;
    apiGet(`/trades?limit=${TRADES_PAGE_SIZE}&offset=${nextOffset}&period=${period}`)
      .then((data) => {
        setTrades((prev) => [...(prev || []), ...data.trades]);
        setTradesTotal(data.total ?? nextOffset + data.trades.length);
      })
      .catch((e) => setError(e.message))
      .finally(() => setTradesLoadingMore(false));
  };

  const hasMoreTrades = !!trades && trades.length < tradesTotal;


  const chartData = useMemo(
    () => (stats?.equity || []).map((p, i) => ({ i, v: p.v })),
    [stats]
  );

  const changeUsd = stats?.period_change_usd ?? 0;
  const changePct = stats?.period_change_pct;
  const up = changeUsd >= 0;

  const openPnl = stats?.open_unrealized_pnl ?? 0;
  const fundingTotal = stats?.funding_fees_total ?? 0;
  const totalPnl = stats?.total_pnl ?? stats?.cumulative_pnl ?? 0;
  const totalUp = totalPnl >= 0;

  const avgTrade = stats && stats.total_trades ? stats.total_net_pnl / stats.total_trades : 0;

  const pf = stats?.profit_factor;
  const pfLabel = pf != null ? pf.toFixed(2) : stats?.gross_profit > 0 ? "∞" : "—";
  const pfClass = pf != null ? (pf >= 1 ? "text-profit" : "text-loss") : stats?.gross_profit > 0 ? "text-profit" : "";
  const winsHidingLoss = !!stats && pf != null && pf < 1 && stats.win_rate >= 50;

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">Не удалось загрузить данные: {error}</div>}

      {/* Hero — реализованный PnL закрытых сделок (всё время), не баланс биржи */}
      <div className="hero">
        <div className="hero-top">
          <span className="hero-label">PnL закрытых сделок</span>
          <div className="period-pills">
            {PERIODS.map((p) => (
              <button
                key={p}
                className={`pill ${p === period ? "pill-active" : ""}`}
                onClick={() => setPeriod(p)}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        {!stats ? (
          <div className="hero-figure hero-loading"><Spinner /></div>
        ) : (
          <>
            <div className="hero-figure">{fmtUsd(stats.cumulative_pnl)}</div>
            <div className={`hero-change ${up ? "text-profit" : "text-loss"}`}>
              {up ? <TrendingUp size={15} /> : <TrendingDown size={15} />}
              <span>{fmtUsd(changeUsd)}</span>
              {changePct != null && <span className="hero-change-pct">({fmtPct(changePct)})</span>}
              <span className="hero-change-period">за период</span>
            </div>
          </>
        )}

        <div className="chart-wrap">
          {stats && chartData.length > 1 ? (
            <ResponsiveContainer width="100%" height={120}>
              <AreaChart data={chartData} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="fillProfit" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#35D68A" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#35D68A" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="fillLoss" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#F1495B" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#F1495B" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="i" hide />
                <Tooltip
                  cursor={{ stroke: "#3A4354", strokeWidth: 1 }}
                  contentStyle={{
                    background: "#1A2029",
                    border: "1px solid #232A35",
                    borderRadius: 8,
                    fontSize: 12,
                    fontFamily: "JetBrains Mono, monospace",
                    color: "#EDEFF3",
                  }}
                  labelFormatter={() => ""}
                  formatter={(v) => [`$${v.toLocaleString()}`, "Накопл. PnL"]}
                />
                <Area
                  type="monotone"
                  dataKey="v"
                  stroke={up ? "#35D68A" : "#F1495B"}
                  strokeWidth={2}
                  fill={up ? "url(#fillProfit)" : "url(#fillLoss)"}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : stats && chartData.length <= 1 ? (
            <div className="chart-empty">Недостаточно закрытых сделок за период для графика</div>
          ) : null}
        </div>
      </div>

      {stats && (
        <div className="section">
          <div className="section-head">
            <span className="section-title">Итого сейчас</span>
          </div>
          <div className="list">
            <StatRow label="Закрытые сделки" hint="всё время">
              <span className={stats.cumulative_pnl >= 0 ? "text-profit" : "text-loss"}>
                {fmtUsd(stats.cumulative_pnl)}
              </span>
            </StatRow>
            <StatRow label="Открытые позиции" hint="нереализованный PnL сейчас">
              <span className={openPnl >= 0 ? "text-profit" : "text-loss"}>{fmtUsd(openPnl)}</span>
            </StatRow>
            <StatRow label="Фандинг" hint="комиссия за удержание позиций, всё время">
              <span className={fundingTotal >= 0 ? "text-profit" : "text-loss"}>{fmtUsd(fundingTotal)}</span>
            </StatRow>
            <StatRow label="Итого">
              <span className={totalUp ? "text-profit" : "text-loss"}>
                {fmtUsd(totalPnl)}
                {stats.total_pnl_pct != null && (
                  <span className="stat-value-sub"> ({fmtPct(stats.total_pnl_pct)} от баланса)</span>
                )}
              </span>
            </StatRow>
          </div>
        </div>
      )}

      {/* Stat chips */}
      <div className="chip-row">
        <div className="chip">
          <span className="chip-label">Win rate</span>
          <span className="chip-value text-profit">{stats ? `${stats.win_rate}%` : "—"}</span>
        </div>
        <div className="chip">
          <span className="chip-label">Сделок</span>
          <span className="chip-value">{stats ? stats.total_trades : "—"}</span>
        </div>
        <div className="chip">
          <span className="chip-label">Сред. сделка</span>
          <span className={`chip-value ${avgTrade >= 0 ? "text-profit" : "text-loss"}`}>
            {stats ? fmtUsd(avgTrade) : "—"}
          </span>
        </div>
      </div>

      {stats && stats.total_trades > 0 && (
        <div className="section">
          <div className="section-head">
            <span className="section-title">Прибыльность</span>
          </div>
          <div className="list">
            <StatRow label="Profit factor" hint="прибыль ÷ убыток, >1 — в плюсе">
              <span className={pfClass}>{pfLabel}</span>
            </StatRow>
            <StatRow label="Средний профит">
              {stats.avg_win != null ? (
                <>
                  <span className="text-profit">{fmtUsd(stats.avg_win)}</span>
                  <span className="stat-value-sub">
                    {stats.winning_trades} {plural(stats.winning_trades, "сделка", "сделки", "сделок")}
                  </span>
                </>
              ) : "—"}
            </StatRow>
            <StatRow label="Средний убыток">
              {stats.avg_loss != null ? (
                <>
                  <span className="text-loss">{fmtUsd(-stats.avg_loss)}</span>
                  <span className="stat-value-sub">
                    {stats.losing_trades} {plural(stats.losing_trades, "сделка", "сделки", "сделок")}
                  </span>
                </>
              ) : "—"}
            </StatRow>
            <StatRow label="Профит / убыток" hint="средний win ÷ средний loss">
              {stats.payoff_ratio != null ? stats.payoff_ratio.toFixed(2) : "—"}
            </StatRow>
            <StatRow label="Валовая прибыль">
              <span className="text-profit">{fmtUsd(stats.gross_profit)}</span>
            </StatRow>
            <StatRow label="Валовый убыток">
              <span className="text-loss">{stats.gross_loss > 0 ? fmtUsd(-stats.gross_loss) : fmtUsd(0)}</span>
            </StatRow>
            <StatRow label="Лучшая сделка">
              <span className={stats.best_trade >= 0 ? "text-profit" : "text-loss"}>{fmtUsd(stats.best_trade)}</span>
            </StatRow>
            <StatRow label="Худшая сделка">
              <span className={stats.worst_trade >= 0 ? "text-profit" : "text-loss"}>{fmtUsd(stats.worst_trade)}</span>
            </StatRow>
          </div>
          {winsHidingLoss && (
            <div className="stat-note">
              Win rate {stats.win_rate}%, но убытки перекрывают прибыль: средний убыток
              больше среднего профита.
            </div>
          )}
        </div>
      )}

      {/* Open positions */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">Открытые позиции</span>
          {positions && <span className="section-count">{positions.length}</span>}
        </div>
        <div className="list">
          {!positions ? (
            <EmptyRow text="Загрузка..." />
          ) : positions.length === 0 ? (
            <EmptyRow text="Нет открытых позиций" />
          ) : (
            positions.map((p) => {
              const tp = p.take_profit_levels?.[0];
              const tpLabel = !p.take_profit_levels?.length
                ? null
                : p.take_profit_levels.length > 1
                ? `${p.take_profit_levels.length} уровня`
                : tp?.price?.toLocaleString();
              // SL в % ROI (с учётом плеча). Если плеча/входа нет — показываем цену.
              const slRoi = slRoiPercent(p);
              const slLabel = slRoi != null ? fmtPct(slRoi) : p.stop_loss_price?.toLocaleString();
              return (
                <div className="row" key={p.order_id}>
                  <div className="row-main">
                    <div className="row-title-line">
                      <span className="symbol">{p.symbol}</span>
                      <SideBadge side={p.side} />
                      {p.leverage && <span className="row-sub row-sub-faint">{p.leverage}x</span>}
                    </div>
                    <div className="row-sub">
                      вход {p.entry_price?.toLocaleString() ?? "—"}
                      {p.mark_price != null && <> · маркировка {p.mark_price.toLocaleString()}</>}
                    </div>
                    {(p.stop_loss_price || tpLabel) && (
                      <div className="row-sub row-sub-faint">
                        {p.stop_loss_price && <>SL {slLabel}</>}
                        {p.stop_loss_price && tpLabel && " · "}
                        {tpLabel && <>TP {tpLabel}</>}
                      </div>
                    )}
                  </div>
                  <div className="row-end">
                    {p.pnl_usd != null && (
                      <span className={`row-usd ${p.pnl_usd >= 0 ? "text-profit" : "text-loss"}`}>
                        {fmtUsd(p.pnl_usd)}
                      </span>
                    )}
                    <PnlTag value={p.pnl_usd ?? 0} pct={p.pnl_pct} />
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* PnL по монетам — раньше это была картинка (generate_stats_card), теперь список */}
      {stats && stats.symbol_pnl.length > 0 && (
        <div className="section">
          <div className="section-head">
            <span className="section-title">PnL по монетам</span>
          </div>
          <div className="list">
            {stats.symbol_pnl.map((s) => (
              <div className="row row-compact" key={s.symbol}>
                <span className="symbol">{s.symbol}</span>
                <span className={`row-usd ${s.pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(s.pnl)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* По стратегиям */}
      {stats && stats.strategy_stats.length > 0 && (
        <div className="section">
          <div className="section-head">
            <span className="section-title">По стратегиям</span>
          </div>
          <div className="list">
            {stats.strategy_stats.map((s) => (
              <div className="row row-compact" key={s.strategy}>
                <div className="row-main">
                  <span className="settings-title">{s.strategy}</span>
                  <div className="row-sub row-sub-faint">
                    PF {s.profit_factor != null ? s.profit_factor.toFixed(2) : s.pnl > 0 ? "∞" : "—"}
                  </div>
                </div>
                <div className="row-end">
                  <span className={`row-usd ${s.pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(s.pnl)}</span>
                  <span className="row-value">
                    <span className="text-profit">{s.win}W</span> / <span className="text-loss">{s.loss}L</span>
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trade history — сгруппирована по дням, отфильтрована тем же
          периодом (1D/1W/1M/ALL), что и график/сводка выше */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">История сделок</span>
          {tradesTotal > 0 && <span className="section-count">{trades?.length ?? 0} / {tradesTotal}</span>}
        </div>
        <TradeHistoryList trades={trades} />
        {hasMoreTrades && (
          <button className="load-more-btn" onClick={loadMoreTrades} disabled={tradesLoadingMore}>
            {tradesLoadingMore ? "Загрузка..." : "Показать ещё"}
          </button>
        )}
      </div>
    </div>
  );
}