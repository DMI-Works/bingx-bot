import { useEffect, useState } from "react";
import { Ban, Check, Gift } from "lucide-react";
import { apiGet, apiPost } from "../../lib/api";
import { fmtDate, fmtUsd, fmtUsdPlain } from "../../lib/format";
import { EmptyRow, Spinner } from "../common";
import RiskNumberRow from "../settings/RiskNumberRow";

export default function AdminTab() {
  const [users, setUsers] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const [billing, setBilling] = useState(null);

  const load = () => Promise.all([apiGet("/admin/users"), apiGet("/admin/billing")])
    .then(([u, b]) => { setUsers(u.users); setBilling(b); })
    .catch((e) => setError(e.message));

  const closeInvoice = async (invoice, action) => {
    setBusyId(invoice.id);
    setError(null);
    try {
      await apiPost(`/admin/invoices/${invoice.id}/${action}`, {});
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  };

  useEffect(() => { load(); }, []);

  const toggleBlock = async (u) => {
    setBusyId(u.id);
    setError(null);
    try {
      await apiPost(`/admin/users/${u.id}/status`, { status: u.status === "active" ? "blocked" : "active" });
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="tab-pane">
      {error && <div className="error-banner">{error}</div>}
      <div className="section">
        <div className="section-head">
          <span className="section-title">Подписка</span>
          {billing && <span className="section-count">{billing.open_invoices.length}</span>}
        </div>
        <div className="list">
          <RiskNumberRow
            endpoint="/admin/billing/config"
            field="fee_percent"
            title="Комиссия с прибыли, %"
            sub="Действует на следующие счета. Уже выставленные не меняются."
            value={billing?.fee_percent}
            format={(v) => `${v}%`}
            onSaved={(cfg) => setBilling((prev) => (prev ? { ...prev, ...cfg } : prev))}
          />
          {billing?.open_invoices.map((i) => (
            <div className="row" key={i.id}>
              <div className="row-main">
                <div className="row-title-line">
                  <span className="symbol">{i.username ? `@${i.username}` : (i.first_name || i.user_id)}</span>
                  <span className="tag tag-danger">{fmtUsdPlain(i.amount_usdt)}</span>
                </div>
                <div className="row-sub">период до {fmtDate(i.period_end)}  ·  прибыль {fmtUsdPlain(i.chargeable)}</div>
              </div>
              <button className="icon-btn" title="Простить счёт" disabled={busyId === i.id} onClick={() => closeInvoice(i, "waive")}>
                <Gift size={15} />
              </button>
              <button className="icon-btn icon-btn-success" style={{ marginLeft: 8 }} title="Отметить оплаченным" disabled={busyId === i.id} onClick={() => closeInvoice(i, "paid")}>
                <Check size={15} />
              </button>
            </div>
          ))}
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Пользователи</span>
          {users && <span className="section-count">{users.length}</span>}
        </div>
        {!users && !error && <div className="hero-loading"><Spinner /></div>}
        {users && (
          <div className="list">
            {!users.length && <EmptyRow text="Пока никого нет" />}
            {users.map((u) => (
              <div className="row" key={u.id}>
                <div className="row-main">
                  <div className="row-title-line">
                    <span className="symbol">{u.username ? `@${u.username}` : (u.first_name || u.id)}</span>
                    <span className={`mode-badge ${u.testnet ? "mode-testnet" : "mode-live"}`}>
                      {u.testnet ? "Testnet" : "Live"}
                    </span>
                    {u.role === "admin" && <span className="tag tag-success">админ</span>}
                    {u.status === "blocked" && <span className="tag tag-danger">заблокирован</span>}
                  </div>
                  <div className="row-sub">
                    {u.session_active ? "торгует" : "нет сессии"}
                    {"  ·  "}позиций {u.open_positions}
                    {"  ·  "}live {fmtUsd(u.live_net_pnl)} ({u.live_trades})
                  </div>
                </div>
                {u.role !== "admin" && (
                  <button
                    className={`icon-btn ${u.status === "active" ? "icon-btn-danger" : "icon-btn-success"}`}
                    disabled={busyId === u.id}
                    title={u.status === "active" ? "Заблокировать новые входы" : "Разблокировать"}
                    onClick={() => toggleBlock(u)}
                  >
                    {u.status === "active" ? <Ban size={15} /> : <Check size={15} />}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
