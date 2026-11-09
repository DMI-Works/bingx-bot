import { useEffect, useState } from "react";
import { apiGet } from "../../lib/api";
import { fmtDate, fmtUsd, fmtUsdPlain } from "../../lib/format";

const STATUS = {
  open: { text: "к оплате", cls: "tag-danger" },
  paid: { text: "оплачен", cls: "tag-success" },
  waived: { text: "прощён", cls: "tag-info" },
};

export default function BillingSection() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/billing").then((d) => !cancelled && setData(d)).catch(() => { /* не блокируем профиль */ });
    return () => { cancelled = true; };
  }, []);

  if (!data || data.exempt) return null;
  const period = data.current_period;

  return (
    <div className="section">
      <div className="section-head">
        <span className="section-title">Подписка</span>
      </div>
      <div className="list">
        <div className="kv-row">
          <span className="kv-label">Тариф</span>
          <span className="kv-value">{data.fee_percent}% от чистой прибыли за неделю</span>
        </div>
        {data.in_trial && (
          <div className="kv-row">
            <span className="kv-label">Бесплатно до</span>
            <span className="kv-value">{fmtDate(data.trial_until)}</span>
          </div>
        )}
        {period && (
          <>
            <div className="kv-row">
              <span className="kv-label">Текущая неделя до {fmtDate(period.end)}</span>
              <span className={`kv-value ${period.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(period.net_pnl)}</span>
            </div>
            <div className="kv-row">
              <span className="kv-label">Ожидаемая комиссия</span>
              <span className="kv-value">{fmtUsdPlain(period.estimated_fee)}</span>
            </div>
          </>
        )}
        {data.carry_pnl < 0 && (
          <div className="kv-row">
            <span className="kv-label">Минус к отыгрышу</span>
            <span className="kv-value text-loss">{fmtUsd(data.carry_pnl)}</span>
          </div>
        )}
        {data.invoices.map((i) => (
          <div className="kv-row" key={i.id}>
            <span className="kv-label">Счёт до {fmtDate(i.period_end)}</span>
            <span className="kv-value">
              {fmtUsdPlain(i.amount_usdt)}{" "}
              <span className={`tag ${STATUS[i.status]?.cls ?? ""}`}>{STATUS[i.status]?.text ?? i.status}</span>
            </span>
          </div>
        ))}
      </div>
      {data.unpaid && (
        <div className="row-sub" style={{ marginTop: 8 }}>
          Для оплаты счёта свяжитесь с администратором. Новые сделки откроются сразу после подтверждения оплаты.
        </div>
      )}
    </div>
  );
}
