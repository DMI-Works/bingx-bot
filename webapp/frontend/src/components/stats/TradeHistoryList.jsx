import { fmtUsd, fmtUsdPlain, fmtTime, groupTradesByDay, tradeCommission } from "../../lib/format";
import { EmptyRow, PnlTag, SideBadge } from "../common";

export default function TradeHistoryList({ trades }) {
  if (!trades) return <div className="list"><EmptyRow text="Загрузка..." /></div>;
  if (!trades.length) return <div className="list"><EmptyRow text="Нет закрытых сделок за этот период" /></div>;

  const groups = groupTradesByDay(trades);

  return (
    <>
      {groups.map((g) => (
        <div className="day-group" key={g.key}>
          <div className="day-sep">
            <span className="day-sep-label">{g.label}</span>
            <span className="day-sep-end">
              <span className={`day-sep-pnl ${g.pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(g.pnl)}</span>
              {g.commission !== 0 && (
                <span className="day-sep-commission">комиссия {fmtUsd(g.commission)}</span>
              )}
            </span>
          </div>
          <div className="list">
            {g.items.map((t) => {
              const commission = tradeCommission(t);
              return (
                <div className="row row-compact" key={t.order_id}>
                  <div className="row-main">
                    <div className="row-title-line">
                      <span className="symbol">{t.symbol}</span>
                      <SideBadge side={t.side} />
                    </div>
                    <div className="row-sub">
                      {fmtTime(t.closed_at)}
                      {commission != null && (
                        <>
                          {"  ·  "}
                          <span className="row-sub-faint" title={
                            t.commission_open != null || t.commission_close != null
                              ? `открытие ${fmtUsdPlain(t.commission_open ?? 0)} · закрытие ${fmtUsdPlain(t.commission_close ?? 0)}`
                              : undefined
                          }>
                            комиссия {fmtUsd(commission)}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="row-end">
                    <span className={`row-usd ${t.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                      {fmtUsd(t.net_pnl)}
                    </span>
                    {t.roe_percent != null && <PnlTag value={t.net_pnl} pct={t.roe_percent} />}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </>
  );
}
