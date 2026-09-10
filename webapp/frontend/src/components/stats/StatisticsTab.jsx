import { useState, useMemo, useEffect } from "react";
import {
  AreaChart, Area, XAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import { TrendingUp, TrendingDown } from "lucide-react";
import { apiGet } from "../../lib/api";
import { fmtUsd, fmtPct, fmtDate } from "../../lib/format";
import { Spinner, EmptyRow, PnlTag, SideBadge } from "../common";

const PERIODS = ["1D", "1W", "1M", "ALL"];

export default function StatisticsTab() {
  const [period, setPeriod] = useState("1W");
  const [stats, setStats] = useState(null);
  const [positions, setPositions] = useState(null);
  const [trades, setTrades] = useState(null);
  const [error, setError] = useState(null);

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
    apiGet("/trades?limit=20")
      .then((data) => !cancelled && setTrades(data.trades))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  const chartData = useMemo(
    () => (stats?.equity || []).map((p, i) => ({ i, v: p.v })),
    [stats]
  );

  const first = chartData[0]?.v ?? 0;
  const last = chartData[chartData.length - 1]?.v ?? stats?.cumulative_pnl ?? 0;
  const changeUsd = chartData.length ? last - first : (stats?.cumulative_pnl || 0);
  const changePct = first !== 0 ? (changeUsd / Math.abs(first)) * 100 : (changeUsd !== 0 ? 100 : 0);
  const up = changeUsd >= 0;

  const avgTrade = stats && stats.total_trades ? stats.total_net_pnl / stats.total_trades : 0;

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">Не удалось загрузить данные: {error}</div>}

      {/* Hero — кумулятивный net PnL закрытых сделок за период, не полный баланс биржи */}
      <div className="hero">
        <div className="hero-top">
          <span className="hero-label">Накопленный PnL</span>
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
              <span className="hero-change-pct">({fmtPct(changePct)})</span>
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
                        {p.stop_loss_price && <>SL {p.stop_loss_price.toLocaleString()}</>}
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
                <span className="settings-title">{s.strategy}</span>
                <span className="row-value">
                  <span className="text-profit">{s.win}W</span> / <span className="text-loss">{s.loss}L</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trade history */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">История сделок</span>
        </div>
        <div className="list">
          {!trades ? (
            <EmptyRow text="Загрузка..." />
          ) : trades.length === 0 ? (
            <EmptyRow text="Нет закрытых сделок" />
          ) : (
            trades.map((t) => (
              <div className="row row-compact" key={t.order_id}>
                <div className="row-main">
                  <div className="row-title-line">
                    <span className="symbol">{t.symbol}</span>
                    <SideBadge side={t.side} />
                  </div>
                  <div className="row-sub">{fmtDate(t.closed_at)}</div>
                </div>
                <div className="row-end">
                  <span className={`row-usd ${t.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                    {fmtUsd(t.net_pnl)}
                  </span>
                  {t.roe_percent != null && <PnlTag value={t.net_pnl} pct={t.roe_percent} />}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
