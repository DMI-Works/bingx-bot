import { fmtUsd, fmtUsdPlain, fmtTime, groupTradesByDay, tradeCommission } from "../../lib/format";
import { EmptyRow, PnlTag, SideBadge } from "../common";
import { t } from "../../i18n";

export default function TradeHistoryList({ trades }) {
  if (!trades) return <div className="list"><EmptyRow text={t("loading")} /></div>;
  if (!trades.length) return <div className="list"><EmptyRow text={t("no_trades_period")} /></div>;

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
                <span className="day-sep-commission">{t("commission", { v: fmtUsd(g.commission) })}</span>
              )}
            </span>
          </div>
          <div className="list">
            {g.items.map((tr) => {
              const commission = tradeCommission(tr);
              return (
                <div className="row row-compact" key={tr.order_id}>
                  <div className="row-main">
                    <div className="row-title-line">
                      <span className="symbol">{tr.symbol}</span>
                      <SideBadge side={tr.side} />
                      {tr.leverage && <span className="row-sub row-sub-faint">{tr.leverage}x</span>}
                      {tr.margin_usdt != null && (
                        <span className="row-sub row-sub-faint">{t("margin", { v: fmtUsdPlain(tr.margin_usdt) })}</span>
                      )}
                    </div>
                    <div className="row-sub">
                      {fmtTime(tr.closed_at)}
                      {commission != null && (
                        <>
                          {"  ·  "}
                          <span className="row-sub-faint" title={
                            tr.commission_open != null || tr.commission_close != null
                              ? t("commission_split", { open: fmtUsdPlain(tr.commission_open ?? 0), close: fmtUsdPlain(tr.commission_close ?? 0) })
                              : undefined
                          }>
                            {t("commission", { v: fmtUsd(commission) })}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="row-end">
                    <span className={`row-usd ${tr.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                      {fmtUsd(tr.net_pnl)}
                    </span>
                    {tr.roe_percent != null && <PnlTag value={tr.net_pnl} pct={tr.roe_percent} />}
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
