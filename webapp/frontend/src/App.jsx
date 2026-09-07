import React, { useState, useMemo, useEffect } from "react";
import {
  AreaChart, Area, XAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import {
  TrendingUp, TrendingDown, ChevronRight, Bell, Shield, Sliders,
  KeyRound, LogOut, AlertTriangle, Wallet, Activity, BarChart3,
  User, SettingsIcon, ArrowUpRight, ArrowDownRight, Circle,
} from "lucide-react";

// ---------------------------------------------------------------------------
// API layer — talks to webapp/backend/api.py, falls back to mocks above if
// the backend isn't reachable yet (useful while working on the UI alone).
// ---------------------------------------------------------------------------

const tg = typeof window !== "undefined" ? window.Telegram?.WebApp : null;

async function apiGet(path) {
  const res = await fetch(`/api${path}`, {
    headers: {
      // Бэкенд валидирует эту строку через HMAC с bot token — см. backend/auth.py
      "X-Telegram-Init-Data": tg?.initData || "",
    },
  });
  if (!res.ok) throw new Error(`API ${path} -> ${res.status}`);
  return res.json();
}


const fmtUsd = (n) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const fmtPct = (n) => `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(2)}%`;

function PnlTag({ value, pct }) {
  const up = value >= 0;
  return (
    <div className={`pnl-tag ${up ? "pnl-up" : "pnl-down"}`}>
      {up ? <ArrowUpRight size={12} strokeWidth={2.5} /> : <ArrowDownRight size={12} strokeWidth={2.5} />}
      <span>{fmtPct(pct)}</span>
    </div>
  );
}

function SideBadge({ side }) {
  return <span className={`side-badge ${side === "LONG" ? "side-long" : "side-short"}`}>{side}</span>;
}

// ---------------------------------------------------------------------------
// Statistics tab
// ---------------------------------------------------------------------------

function StatisticsTab() {
  const [period, setPeriod] = useState("1W");
  const [stats, setStats] = useState(null);
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    Promise.all([
      apiGet(`/stats?period=${period}`),
      apiGet(`/positions`),
      apiGet(`/trades?period=${period}&limit=20`),
    ]).then(([nextStats, nextPositions, nextTrades]) => {
      if (cancelled) return;
      setStats(nextStats);
      setPositions(nextPositions || []);
      setTrades(nextTrades || []);
    }).catch((err) => {
      if (!cancelled) setError(err.message || "Не удалось получить данные");
    });
    return () => { cancelled = true; };
  }, [period]);

  const equity = stats?.equity || [];
  const chartData = useMemo(() => equity.map((point, i) => ({
    i, v: Number(point.v || 0), t: point.t,
  })), [equity]);
  const first = chartData[0]?.v || 0;
  const last = chartData[chartData.length - 1]?.v || 0;
  const changeUsd = last - first;
  const changePct = first ? (changeUsd / first) * 100 : 0;
  const up = changeUsd >= 0;
  const summary = stats?.summary || {};

  return (
    <div className="tab-pane">
      {error && <div className="error-box">{error}</div>}
      <div className="hero">
        <div className="hero-top">
          <span className="hero-label">Общий баланс</span>
          <div className="period-pills">
            {["1D", "1W", "1M", "ALL"].map((p) => (
              <button key={p} className={`pill ${p === period ? "pill-active" : ""}`} onClick={() => setPeriod(p)}>{p}</button>
            ))}
          </div>
        </div>
        <div className="hero-figure">${last.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
        <div className={`hero-change ${up ? "text-profit" : "text-loss"}`}>
          {up ? <TrendingUp size={15} /> : <TrendingDown size={15} />}
          <span>{fmtUsd(changeUsd)}</span>
          <span className="hero-change-pct">({fmtPct(changePct)})</span>
          <span className="hero-change-period">за период</span>
        </div>
        <div className="chart-wrap">
          {chartData.length ? (
            <ResponsiveContainer width="100%" height={120}>
              <AreaChart data={chartData} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
                <XAxis dataKey="i" hide />
                <Tooltip formatter={(v) => [`$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}`, "Баланс"]} />
                <Area type="monotone" dataKey="v" stroke={up ? "#35D68A" : "#F1495B"} strokeWidth={2} fillOpacity={0.15} />
              </AreaChart>
            </ResponsiveContainer>
          ) : <div className="empty-state">Нет данных по балансу за период</div>}
        </div>
      </div>

      <div className="chip-row">
        <div className="chip"><span className="chip-label">Win rate</span><span className="chip-value text-profit">{Number(summary.win_rate || 0).toFixed(1)}%</span></div>
        <div className="chip"><span className="chip-label">Сделок</span><span className="chip-value">{summary.total_trades || 0}</span></div>
        <div className="chip"><span className="chip-label">PnL</span><span className={`chip-value ${Number(summary.total_pnl || 0) >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(Number(summary.total_pnl || 0))}</span></div>
      </div>

      <div className="section">
        <div className="section-head"><span className="section-title">Открытые позиции</span><span className="section-count">{positions.length}</span></div>
        <div className="list">
          {positions.length ? positions.map((p, idx) => (
            <div className="row" key={`${p.symbol}-${p.side}-${idx}`}>
              <div className="row-main">
                <div className="row-title-line"><span className="symbol">{p.symbol}</span><SideBadge side={p.side} /></div>
                <div className="row-sub">вход {Number(p.entry || 0).toLocaleString()} · маркировка {Number(p.mark || 0).toLocaleString()}</div>
                <div className="row-sub row-sub-faint">SL {p.sl == null ? "—" : Number(p.sl).toLocaleString()} · TP {p.tp == null ? "—" : Number(p.tp).toLocaleString()}</div>
              </div>
              <div className="row-end"><span className={`row-usd ${Number(p.pnl_usdt || 0) >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(Number(p.pnl_usdt || 0))}</span><PnlTag value={Number(p.pnl_usdt || 0)} pct={Number(p.pnl_pct || 0)} /></div>
            </div>
          )) : <div className="empty-state">Нет открытых позиций</div>}
        </div>
      </div>

      <div className="section">
        <div className="section-head"><span className="section-title">История сделок</span></div>
        <div className="list">
          {trades.length ? trades.map((t, idx) => (
            <div className="row row-compact" key={`${t.id || t.order_id || idx}`}>
              <div className="row-main"><div className="row-title-line"><span className="symbol">{t.symbol}</span><SideBadge side={t.side} /></div><div className="row-sub">{t.closed_at || t.created_at || "—"}</div></div>
              <div className="row-end"><span className={`row-usd ${Number(t.pnl_usdt || 0) >= 0 ? "text-profit" : "text-loss"}`}>{fmtUsd(Number(t.pnl_usdt || 0))}</span></div>
            </div>
          )) : <div className="empty-state">Нет закрытых сделок за период</div>}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Profile tab
// ---------------------------------------------------------------------------

function ProfileTab() {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    apiGet("/profile").then(setProfile).catch((err) => setError(err.message || "Не удалось получить профиль"));
  }, []);
  const user = profile?.telegram_user || {};
  const balance = profile?.balance || {};
  const available = Number(balance.availableMargin ?? balance.availableBalance ?? balance.free ?? 0);
  const total = Number(balance.equity ?? balance.balance ?? balance.total ?? 0);
  const inPositions = Math.max(0, total - available);
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ") || user.username || "Telegram пользователь";
  const initials = name.split(/\s+/).map((x) => x[0]).join("").slice(0, 2).toUpperCase();
  return (
    <div className="tab-pane">
      {error && <div className="error-box">{error}</div>}
      <div className="profile-head"><div className="avatar">{initials || "TG"}</div><div><div className="profile-name">{name}</div><div className="profile-handle">{user.username ? `@${user.username}` : ""}</div></div></div>
      <div className="section"><div className="section-head"><span className="section-title">Подключение к бирже</span></div><div className="list"><div className="row"><div className="row-main"><div className="row-title-line"><span className="symbol">{profile?.exchange || "BingX"}</span><span className={`mode-badge ${profile?.mode === "live" ? "mode-live" : "mode-testnet"}`}>{profile?.mode === "live" ? "Live" : "Testnet"}</span></div><div className="row-sub">API подключён · данные получены напрямую из BingX</div></div><ChevronRight size={16} className="chev" /></div></div></div>
      <div className="section"><div className="section-head"><span className="section-title">Баланс аккаунта</span></div><div className="list"><div className="kv-row"><span className="kv-label">Доступно</span><span className="kv-value">${available.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></div><div className="kv-row"><span className="kv-label">В позициях</span><span className="kv-value">${inPositions.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></div><div className="kv-row"><span className="kv-label">Всего</span><span className="kv-value kv-value-strong">${total.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></div></div></div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Settings tab
// ---------------------------------------------------------------------------

function ToggleRow({ icon: Icon, title, sub, defaultOn = true }) {
  const [on, setOn] = useState(defaultOn);
  return (
    <div className="row">
      <div className="row-icon">
        <Icon size={17} />
      </div>
      <div className="row-main">
        <div className="row-title-line">
          <span className="settings-title">{title}</span>
        </div>
        {sub && <div className="row-sub">{sub}</div>}
      </div>
      <button
        className={`switch ${on ? "switch-on" : ""}`}
        onClick={() => setOn(!on)}
        aria-label={title}
      >
        <span className="switch-knob" />
      </button>
    </div>
  );
}

function LinkRow({ icon: Icon, title, value }) {
  return (
    <div className="row">
      <div className="row-icon">
        <Icon size={17} />
      </div>
      <div className="row-main">
        <span className="settings-title">{title}</span>
      </div>
      {value && <span className="row-value">{value}</span>}
      <ChevronRight size={16} className="chev" />
    </div>
  );
}

function SettingsTab() {
  return (
    <div className="tab-pane">
      <div className="section">
        <div className="section-head">
          <span className="section-title">Риск-менеджмент</span>
        </div>
        <div className="list">
          <LinkRow icon={Shield} title="Макс. открытых позиций" value="3" />
          <LinkRow icon={Sliders} title="Риск на сделку" value="2%" />
          <LinkRow icon={AlertTriangle} title="Дневной лимит убытка" value="5%" />
          <LinkRow icon={Activity} title="Cooldown после убытка" value="30 мин" />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Stop Loss / Take Profit</span>
        </div>
        <div className="list">
          <LinkRow icon={TrendingDown} title="Тип Stop Loss" value="ATR" />
          <LinkRow icon={TrendingUp} title="Уровни Take Profit" value="2" />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Уведомления</span>
        </div>
        <div className="list">
          <ToggleRow icon={Bell} title="Открытие / закрытие позиций" defaultOn />
          <ToggleRow icon={Bell} title="Срабатывание SL / TP" defaultOn />
          <ToggleRow icon={Bell} title="Ошибки и сбои" defaultOn />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Доступ</span>
        </div>
        <div className="list">
          <LinkRow icon={KeyRound} title="API-ключ BingX" value="•••• 84f2" />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title-danger">Опасная зона</span>
        </div>
        <button className="danger-btn danger-btn-solid">
          <AlertTriangle size={16} />
          Аварийная остановка
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// App shell — bottom tab bar, Telegram-style
// ---------------------------------------------------------------------------

const TABS = [
  { id: "stats", label: "Статистика", icon: BarChart3, render: StatisticsTab },
  { id: "profile", label: "Профиль", icon: User, render: ProfileTab },
  { id: "settings", label: "Настройки", icon: SettingsIcon, render: SettingsTab },
];

export default function App() {
  const [active, setActive] = useState("stats");
  const ActiveTab = TABS.find((t) => t.id === active).render;

  return (
    <div className="app-root">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

        * { box-sizing: border-box; }

        .app-root {
          --bg: #0B0E13;
          --surface: #12161D;
          --surface-2: #1A2029;
          --border: #212832;
          --text: #EDEFF3;
          --text-dim: #8890A0;
          --text-faint: #545D6E;
          --profit: #35D68A;
          --loss: #F1495B;
          --accent: #6C8CFF;
          --accent-soft: rgba(108,140,255,0.14);

          width: 100%;
          max-width: 480px;
          margin: 0 auto;
          background: var(--bg);
          color: var(--text);
          font-family: 'Inter', system-ui, sans-serif;
          display: flex;
          flex-direction: column;
          /* dvh — учитывает Telegram viewport, а не просто окно браузера */
          height: 100dvh;
        }

        .text-profit { color: var(--profit); }
        .text-loss { color: var(--loss); }
        .error-box { margin-bottom: 12px; padding: 10px 12px; border: 1px solid rgba(241,73,91,.35); border-radius: 8px; color: var(--loss); background: rgba(241,73,91,.08); font-size: 12px; }
        .empty-state { padding: 16px; color: var(--text-faint); font-size: 12px; text-align: center; }

        .tab-pane {
          flex: 1;
          overflow-y: auto;
          padding: 20px 16px 12px;
        }
        .tab-pane::-webkit-scrollbar { display: none; }

        /* Hero */
        .hero { padding-bottom: 8px; }
        .hero-top {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 10px;
        }
        .hero-label {
          font-size: 13px;
          color: var(--text-dim);
          font-weight: 500;
        }
        .period-pills {
          display: flex;
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 8px;
          padding: 2px;
          gap: 2px;
        }
        .pill {
          border: none;
          background: transparent;
          color: var(--text-faint);
          font-size: 11px;
          font-weight: 600;
          font-family: 'JetBrains Mono', monospace;
          padding: 4px 8px;
          border-radius: 6px;
          cursor: pointer;
        }
        .pill-active {
          background: var(--surface-2);
          color: var(--text);
        }
        .hero-figure {
          font-family: 'JetBrains Mono', monospace;
          font-size: 34px;
          font-weight: 700;
          letter-spacing: -0.02em;
          line-height: 1.1;
        }
        .hero-change {
          display: flex;
          align-items: center;
          gap: 4px;
          font-size: 13px;
          font-weight: 600;
          font-family: 'JetBrains Mono', monospace;
          margin-top: 6px;
        }
        .hero-change-pct { font-weight: 500; opacity: 0.85; }
        .hero-change-period {
          font-family: 'Inter', sans-serif;
          font-weight: 400;
          color: var(--text-faint);
          margin-left: 2px;
        }
        .chart-wrap { margin-top: 6px; margin-left: -4px; margin-right: -4px; }

        /* Chips */
        .chip-row {
          display: flex;
          gap: 8px;
          margin: 18px 0 22px;
        }
        .chip {
          flex: 1;
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 10px;
          padding: 10px 12px;
          display: flex;
          flex-direction: column;
          gap: 4px;
        }
        .chip-label { font-size: 11px; color: var(--text-dim); }
        .chip-value {
          font-family: 'JetBrains Mono', monospace;
          font-size: 16px;
          font-weight: 700;
        }

        /* Sections */
        .section { margin-bottom: 22px; }
        .section-head {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          margin-bottom: 8px;
          padding: 0 2px;
        }
        .section-title { font-size: 14px; font-weight: 600; color: var(--text); }
        .section-title-danger { font-size: 14px; font-weight: 600; color: var(--loss); }
        .section-count {
          font-family: 'JetBrains Mono', monospace;
          font-size: 12px;
          color: var(--text-faint);
        }

        .list {
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 12px;
          overflow: hidden;
        }
        .row {
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 12px 14px;
          border-bottom: 1px solid var(--border);
        }
        .row:last-child { border-bottom: none; }
        .row-compact { padding: 10px 14px; }
        .row-icon {
          width: 30px;
          height: 30px;
          border-radius: 8px;
          background: var(--surface-2);
          color: var(--text-dim);
          display: flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .row-main { flex: 1; min-width: 0; }
        .row-title-line { display: flex; align-items: center; gap: 6px; }
        .symbol { font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600; }
        .settings-title { font-size: 14px; font-weight: 500; }
        .row-sub { font-size: 12px; color: var(--text-dim); margin-top: 2px; font-family: 'JetBrains Mono', monospace; }
        .row-sub-faint { color: var(--text-faint); }
        .row-end { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
        .row-usd { font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 700; }
        .row-value { font-family: 'JetBrains Mono', monospace; font-size: 13px; color: var(--text-dim); margin-right: 2px; }
        .chev { color: var(--text-faint); flex-shrink: 0; }

        .side-badge {
          font-size: 10px;
          font-weight: 700;
          letter-spacing: 0.02em;
          padding: 1px 6px;
          border-radius: 4px;
          font-family: 'JetBrains Mono', monospace;
        }
        .side-long { background: rgba(53,214,138,0.14); color: var(--profit); }
        .side-short { background: rgba(241,73,91,0.14); color: var(--loss); }

        .mode-badge {
          font-size: 10px;
          font-weight: 700;
          padding: 1px 6px;
          border-radius: 4px;
        }
        .mode-testnet { background: rgba(108,140,255,0.14); color: var(--accent); }

        .pnl-tag {
          display: flex;
          align-items: center;
          gap: 2px;
          font-size: 11px;
          font-weight: 600;
          font-family: 'JetBrains Mono', monospace;
        }
        .pnl-up { color: var(--profit); }
        .pnl-down { color: var(--loss); }

        /* Profile */
        .profile-head {
          display: flex;
          align-items: center;
          gap: 12px;
          margin-bottom: 22px;
        }
        .avatar {
          width: 52px;
          height: 52px;
          border-radius: 14px;
          background: var(--accent-soft);
          color: var(--accent);
          display: flex;
          align-items: center;
          justify-content: center;
          font-weight: 700;
          font-size: 16px;
          font-family: 'JetBrains Mono', monospace;
        }
        .profile-name { font-size: 16px; font-weight: 600; }
        .profile-handle { font-size: 13px; color: var(--text-dim); margin-top: 1px; }

        .kv-row {
          display: flex;
          justify-content: space-between;
          padding: 11px 14px;
          border-bottom: 1px solid var(--border);
        }
        .kv-row:last-child { border-bottom: none; }
        .kv-label { font-size: 13px; color: var(--text-dim); }
        .kv-value { font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600; }
        .kv-value-strong { color: var(--text); font-size: 14px; }

        /* Buttons */
        .danger-btn {
          width: 100%;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          padding: 12px;
          border-radius: 12px;
          border: 1px solid var(--border);
          background: var(--surface);
          color: var(--loss);
          font-size: 13px;
          font-weight: 600;
          cursor: pointer;
          margin-top: 4px;
        }
        .danger-btn-solid {
          background: rgba(241,73,91,0.12);
          border-color: rgba(241,73,91,0.3);
        }

        /* Toggle switch */
        .switch {
          width: 38px;
          height: 22px;
          border-radius: 999px;
          background: var(--surface-2);
          border: 1px solid var(--border);
          position: relative;
          cursor: pointer;
          flex-shrink: 0;
          transition: background 0.15s ease;
        }
        .switch-on { background: var(--accent); border-color: var(--accent); }
        .switch-knob {
          position: absolute;
          top: 2px;
          left: 2px;
          width: 16px;
          height: 16px;
          border-radius: 50%;
          background: #fff;
          transition: transform 0.15s ease;
        }
        .switch-on .switch-knob { transform: translateX(16px); }

        /* Bottom tab bar */
        .tabbar {
          display: flex;
          border-top: 1px solid var(--border);
          background: var(--surface);
          padding: 8px 8px calc(8px + env(safe-area-inset-bottom, 0px));
        }
        .tabbar-btn {
          flex: 1;
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 3px;
          background: none;
          border: none;
          color: var(--text-faint);
          font-size: 10.5px;
          font-weight: 500;
          padding: 4px 0;
          cursor: pointer;
        }
        .tabbar-btn.active { color: var(--accent); }
      `}</style>

      <ActiveTab />

      <nav className="tabbar">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              className={`tabbar-btn ${active === t.id ? "active" : ""}`}
              onClick={() => setActive(t.id)}
            >
              <Icon size={20} strokeWidth={active === t.id ? 2.3 : 1.8} />
              <span>{t.label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
