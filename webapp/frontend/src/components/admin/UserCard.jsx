import { useState } from "react";
import { Ban, Check, ChevronRight } from "lucide-react";
import { apiGet } from "../../lib/api";
import { fmtDate, fmtUsd, fmtUsdPlain } from "../../lib/format";
import { Spinner } from "../common";

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
  const keyText = (k) => (k.connected ? `подключён${k.key_suffix ? ` •••• ${k.key_suffix}` : ""}` : "нет");
  const withdraw = ["live", "testnet"].filter((m) => d.keys[m].can_withdraw);
  const b = d.billing;

  return (
    <>
      {withdraw.length > 0 && (
        <div className="error-banner" style={{ marginTop: 10 }}>
          У ключа {withdraw.join(" и ")} разрешён вывод средств — опасно, попросите пересоздать ключ без вывода.
        </div>
      )}

      <Block title="Счёт">
        <Kv label="Режим" value={modeLabel} />
        {d.account ? (
          <>
            <Kv label="Баланс (equity)" value={d.account.equity != null ? fmtUsdPlain(d.account.equity) : "—"} />
            {d.account.unrealized_pnl != null && (
              <Kv label="Открытый PnL" value={fmtUsd(d.account.unrealized_pnl)} cls={pnlClass(d.account.unrealized_pnl)} />
            )}
            {d.account.error && <Kv label="Биржа" value={d.account.error} cls="text-loss" />}
          </>
        ) : (
          <Kv label="Сессия" value="не запущена (нет ключей для режима)" />
        )}
      </Block>

      <Block title={`Результат · ${modeLabel}`}>
        <Kv label="Сегодня" value={fmtUsd(s.today)} cls={pnlClass(s.today)} />
        <Kv label="7 дней" value={fmtUsd(s.d7)} cls={pnlClass(s.d7)} />
        <Kv label="30 дней" value={fmtUsd(s.d30)} cls={pnlClass(s.d30)} />
        <Kv label="За всё время" value={fmtUsd(s.net_pnl)} cls={pnlClass(s.net_pnl)} />
        <Kv label="Сделок / винрейт" value={s.trades ? `${s.trades} / ${s.winrate}%` : "0"} />
        {s.trades > 0 && (
          <>
            <Kv label="Лучшая / худшая" value={`${fmtUsd(s.best)} / ${fmtUsd(s.worst)}`} />
            <Kv label="Комиссии" value={fmtUsd(s.fees)} cls="text-loss" />
            <Kv label="Последняя сделка" value={fmtDate(s.last_trade_at)} />
          </>
        )}
        {s.by_strategy.map((st) => (
          <Kv
            key={st.strategy}
            label={`${st.strategy} · ${st.trades} сд., ${Math.round((st.wins / st.trades) * 100)}%`}
            value={fmtUsd(st.net_pnl)}
            cls={pnlClass(st.net_pnl)}
          />
        ))}
        {other.trades > 0 && (
          <Kv
            label={`${d.mode === "live" ? "Testnet" : "Live"} за всё время · ${other.trades} сд.`}
            value={fmtUsd(other.net_pnl)}
            cls={pnlClass(other.net_pnl)}
          />
        )}
      </Block>

      {d.positions.length > 0 && (
        <Block title={`Открытые позиции · ${d.positions.length}`}>
          {d.positions.map((p) => (
            <Kv
              key={`${p.symbol}-${p.side}`}
              label={`${p.symbol} ${p.side} · ${p.strategy || "вручную"}`}
              value={p.pnl_usd != null ? fmtUsd(p.pnl_usd) : "—"}
              cls={pnlClass(p.pnl_usd)}
            />
          ))}
        </Block>
      )}

      <Block title="Подписка">
        {!b ? (
          <Kv label="Статус" value="—" />
        ) : b.exempt ? (
          <Kv label="Статус" value="без оплаты (админ)" />
        ) : (
          <>
            {b.in_trial && <Kv label="Пробный период до" value={fmtDate(b.trial_until)} />}
            {b.current_period && (
              <>
                <Kv label="Текущая неделя" value={fmtUsd(b.current_period.net_pnl)} cls={pnlClass(b.current_period.net_pnl)} />
                <Kv label="Ожидаемая комиссия" value={fmtUsdPlain(b.current_period.estimated_fee)} />
              </>
            )}
            {b.carry_pnl < 0 && <Kv label="Минус к отыгрышу" value={fmtUsd(b.carry_pnl)} cls="text-loss" />}
            <Kv label="Оплата" value={b.unpaid ? "есть неоплаченный счёт" : "долгов нет"} cls={b.unpaid ? "text-loss" : ""} />
          </>
        )}
      </Block>

      <Block title="Профиль">
        <Kv label="Telegram id" value={d.id} />
        <Kv label="Регистрация" value={d.created_at ? fmtDate(d.created_at) : "—"} />
        <Kv label="Ключ Live" value={keyText(d.keys.live)} />
        <Kv label="Ключ Testnet" value={keyText(d.keys.testnet)} />
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
            {u.role === "admin" && <span className="tag tag-success">админ</span>}
            {u.status === "blocked" && <span className="tag tag-danger">заблокирован</span>}
          </div>
          <div className="row-sub">
            {u.session_active ? "торгует" : "нет сессии"}
            {"  ·  "}позиций {u.open_positions}
            {"  ·  "}<span className={pnlClass(u.net_pnl)}>{fmtUsd(u.net_pnl)}</span> ({u.trades} сд.)
          </div>
        </div>
        {u.role !== "admin" && (
          <button
            className={`icon-btn ${u.status === "active" ? "icon-btn-danger" : "icon-btn-success"}`}
            disabled={busy}
            title={u.status === "active" ? "Заблокировать новые входы" : "Разблокировать"}
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
