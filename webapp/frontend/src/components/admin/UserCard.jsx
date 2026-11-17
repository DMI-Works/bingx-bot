import { useState } from "react";
import { Ban, Check, ChevronRight } from "lucide-react";
import { apiGet } from "../../lib/api";
import { fmtDate, fmtUsd, fmtUsdPlain } from "../../lib/format";
import { Spinner } from "../common";
import { t } from "../../i18n";

const pnlClass = (v) => (v == null ? "" : v >= 0 ? "text-profit" : "text-loss");

function Kv({ label, value, cls = "" }) {
  return (
    <div className="kv-row">
      <span className="kv-label">{label}</span>
      <span className={`kv-value ${cls}`}>{value}</span>
    </div>
  );
}

function Block({ title, children }) {
  return (
    <div style={{ marginTop: 12 }}>
      <div className="row-sub" style={{ marginBottom: 4 }}>{title}</div>
      <div className="list">{children}</div>
    </div>
  );
}

function UserDetails({ d }) {
  const modeLabel = d.mode === "live" ? "Live" : "Testnet";
  const s = d.stats[d.mode];
  const other = d.stats[d.mode === "live" ? "testnet" : "live"];
  const keyText = (k) => (k.connected ? t("key_connected_k", { suffix: k.key_suffix ? ` •••• ${k.key_suffix}` : "" }) : t("no"));
  const withdraw = ["live", "testnet"].filter((m) => d.keys[m].can_withdraw);
  const b = d.billing;

  return (
    <>
      {withdraw.length > 0 && (
        <div className="error-banner" style={{ marginTop: 10 }}>
          {t("withdraw_warning", { modes: withdraw.join(` ${t("and")} `) })}
        </div>
      )}

      <Block title={t("account")}>
        <Kv label={t("mode")} value={modeLabel} />
        {d.account ? (
          <>
            <Kv label={t("equity")} value={d.account.equity != null ? fmtUsdPlain(d.account.equity) : "—"} />
            {d.account.unrealized_pnl != null && (
              <Kv label={t("open_pnl")} value={fmtUsd(d.account.unrealized_pnl)} cls={pnlClass(d.account.unrealized_pnl)} />
            )}
            {d.account.error && <Kv label={t("exchange")} value={d.account.error} cls="text-loss" />}
          </>
        ) : (
          <Kv label={t("session")} value={t("session_not_started")} />
        )}
      </Block>

      <Block title={t("result", { mode: modeLabel })}>
        <Kv label={t("today")} value={fmtUsd(s.today)} cls={pnlClass(s.today)} />
        <Kv label={t("d7")} value={fmtUsd(s.d7)} cls={pnlClass(s.d7)} />
        <Kv label={t("d30")} value={fmtUsd(s.d30)} cls={pnlClass(s.d30)} />
        <Kv label={t("all_time_cap")} value={fmtUsd(s.net_pnl)} cls={pnlClass(s.net_pnl)} />
        <Kv label={t("trades_wr")} value={s.trades ? `${s.trades} / ${s.winrate}%` : "0"} />
        {s.trades > 0 && (
          <>
            <Kv label={t("best_worst")} value={`${fmtUsd(s.best)} / ${fmtUsd(s.worst)}`} />
            <Kv label={t("fees")} value={fmtUsd(s.fees)} cls="text-loss" />
            <Kv label={t("last_trade_at")} value={fmtDate(s.last_trade_at)} />
          </>
        )}
        {s.by_strategy.map((st) => (
          <Kv
            key={st.strategy}
            label={t("strat_line", { s: st.strategy, n: st.trades, wr: Math.round((st.wins / st.trades) * 100) })}
            value={fmtUsd(st.net_pnl)}
            cls={pnlClass(st.net_pnl)}
          />
        ))}
        {other.trades > 0 && (
          <Kv
            label={t("other_all_time", { mode: d.mode === "live" ? "Testnet" : "Live", n: other.trades })}
            value={fmtUsd(other.net_pnl)}
            cls={pnlClass(other.net_pnl)}
          />
        )}
      </Block>

      {d.positions.length > 0 && (
        <Block title={t("open_positions_n", { n: d.positions.length })}>
          {d.positions.map((p) => (
            <Kv
              key={`${p.symbol}-${p.side}`}
              label={`${p.symbol} ${p.side} · ${p.strategy || t("manual")}`}
              value={p.pnl_usd != null ? fmtUsd(p.pnl_usd) : "—"}
              cls={pnlClass(p.pnl_usd)}
            />
          ))}
        </Block>
      )}

      <Block title={t("sub_title")}>
        {!b ? (
          <Kv label={t("status")} value="—" />
        ) : b.exempt ? (
          <Kv label={t("status")} value={t("no_payment_admin")} />
        ) : (
          <>
            {b.in_trial && <Kv label={t("trial_until")} value={fmtDate(b.trial_until)} />}
            {b.current_period && (
              <>
                <Kv label={t("current_week")} value={fmtUsd(b.current_period.net_pnl)} cls={pnlClass(b.current_period.net_pnl)} />
                <Kv label={t("sub_est_fee")} value={fmtUsdPlain(b.current_period.estimated_fee)} />
              </>
            )}
            {b.carry_pnl < 0 && <Kv label={t("sub_carry")} value={fmtUsd(b.carry_pnl)} cls="text-loss" />}
            <Kv label={t("payment")} value={t(b.unpaid ? "unpaid_invoice" : "no_debts")} cls={b.unpaid ? "text-loss" : ""} />
          </>
        )}
      </Block>

      <Block title={t("profile")}>
        <Kv label="Telegram id" value={d.id} />
        <Kv label={t("registered")} value={d.created_at ? fmtDate(d.created_at) : "—"} />
        <Kv label={t("key_live")} value={keyText(d.keys.live)} />
        <Kv label={t("key_testnet")} value={keyText(d.keys.testnet)} />
      </Block>
    </>
  );
}

export default function UserCard({ user: u, busy, onToggleBlock }) {
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState(null);
  const [error, setError] = useState(null);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) {
      setError(null);
      apiGet(`/admin/users/${u.id}`).then(setDetails).catch((e) => setError(e.message));
    }
  };

  return (
    <div className="strategy-card">
      <div className="row" onClick={toggle} style={{ cursor: "pointer", borderBottom: open ? undefined : "none" }}>
        <div className="row-main">
          <div className="row-title-line">
            <span className="symbol">{u.username ? `@${u.username}` : (u.first_name || u.id)}</span>
            <span className={`mode-badge ${u.testnet ? "mode-testnet" : "mode-live"}`}>
              {u.testnet ? "Testnet" : "Live"}
            </span>
            {u.role === "admin" && <span className="tag tag-success">{t("admin_tag")}</span>}
            {u.status === "blocked" && <span className="tag tag-danger">{t("blocked_tag")}</span>}
          </div>
          <div className="row-sub">
            {t(u.session_active ? "trading_tag" : "no_session_tag")}
            {"  ·  "}{t("positions_n", { n: u.open_positions })}
            {"  ·  "}<span className={pnlClass(u.net_pnl)}>{fmtUsd(u.net_pnl)}</span> {t("trades_short", { n: u.trades })}
          </div>
        </div>
        {u.role !== "admin" && (
          <button
            className={`icon-btn ${u.status === "active" ? "icon-btn-danger" : "icon-btn-success"}`}
            disabled={busy}
            title={t(u.status === "active" ? "block_entries" : "unblock")}
            onClick={(e) => { e.stopPropagation(); onToggleBlock(u); }}
          >
            {u.status === "active" ? <Ban size={15} /> : <Check size={15} />}
          </button>
        )}
        <ChevronRight size={16} className="chev" style={{ marginLeft: 8, transform: open ? "rotate(90deg)" : "none" }} />
      </div>

      {open && (
        <div className="strategy-body">
          {error && <div className="error-banner">{error}</div>}
          {!details && !error && <div className="hero-loading"><Spinner /></div>}
          {details && <UserDetails d={details} />}
        </div>
      )}
    </div>
  );
}
