import { useEffect, useState } from "react";
import { Ban, Check } from "lucide-react";
import { apiGet, apiPost } from "../../lib/api";
import { fmtUsd } from "../../lib/format";
import { EmptyRow, Spinner } from "../common";

export default function AdminTab() {
  const [users, setUsers] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const load = () => apiGet("/admin/users")
    .then((data) => setUsers(data.users))
    .catch((e) => setError(e.message));

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
