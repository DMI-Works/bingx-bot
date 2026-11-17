import { useEffect, useState } from "react";
import { apiGet } from "../../lib/api";
import { fmtDate, fmtUsd, fmtUsdPlain } from "../../lib/format";
import { t } from "../../i18n";

// text — ключ перевода
const STATUS = {
  open: { text: "inv_open", cls: "tag-danger" },
  paid: { text: "inv_paid", cls: "tag-success" },
  waived: { text: "inv_waived", cls: "tag-info" },
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
        <span className="section-title">{t("sub_title")}</span>
      </div>
      <div className="list">
        <div className="kv-row">
          <span className="kv-label">{t("sub_plan")}</span>
          <span className="kv-value">{t("sub_plan_value", { fee: data.fee_percent })}</span>
        </div>
        {data.in_trial && (
          <div className="kv-row">
            <span className="kv-label">{t("sub_free_until")}</span>
            <span className="kv-value">{fmtDate(data.trial_until)}</span>
          </div>
        )}
        {period && (
          <>
            <div className="kv-row">
              <span className="kv-label">{t("sub_week_until", { date: fmtDate(period.end) })}</span>
              <span className={`kv-value ${period.net_pnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(period.net_pnl)}</span>
            </div>
            <div className="kv-row">
              <span className="kv-label">{t("sub_est_fee")}</span>
              <span className="kv-value">{fmtUsdPlain(period.estimated_fee)}</span>
            </div>
          </>
        )}
        {data.carry_pnl < 0 && (
          <div className="kv-row">
            <span className="kv-label">{t("sub_carry")}</span>
            <span className="kv-value text-loss">{fmtUsd(data.carry_pnl)}</span>
          </div>
        )}
        {data.invoices.map((i) => (
          <div className="kv-row" key={i.id}>
            <span className="kv-label">{t("sub_invoice_until", { date: fmtDate(i.period_end) })}</span>
            <span className="kv-value">
              {fmtUsdPlain(i.amount_usdt)}{" "}
              <span className={`tag ${STATUS[i.status]?.cls ?? ""}`}>{STATUS[i.status] ? t(STATUS[i.status].text) : i.status}</span>
            </span>
          </div>
        ))}
      </div>
      {data.unpaid && (
        <div className="row-sub" style={{ marginTop: 8 }}>
          {t("sub_pay_hint")}
        </div>
      )}
    </div>
  );
}
