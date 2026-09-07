import React, { useState, useMemo } from "react";
import {
  AreaChart, Area, XAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import {
  TrendingUp, TrendingDown, ChevronRight, Bell, Shield, Sliders,
  KeyRound, LogOut, AlertTriangle, Wallet, Activity, BarChart3,
  User, SettingsIcon, ArrowUpRight, ArrowDownRight, Circle,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Mock data — swap for real API responses from webapp/backend later
// ---------------------------------------------------------------------------

const EQUITY_SERIES = {
  "1D": [
    12138, 12160, 12142, 12190, 12210, 12180, 12240, 12270, 12255, 12310,
    12290, 12340, 12380, 12360, 12410, 12440, 12480,
  ],
  "1W": [
    11820, 11940, 11890, 12010, 12080, 12040, 12160, 12210, 12180, 12290,
    12340, 12310, 12400, 12440, 12480,
  ],
  "1M": [
    10600, 10820, 10740, 11020, 11180, 11090, 11340, 11500, 11420, 11680,
    11840, 11790, 12040, 12200, 12480,
  ],
  ALL: [
    8200, 8600, 8420, 9040, 9380, 9210, 9740, 10120, 9960, 10480, 10820,
    10690, 11340, 11980, 12480,
  ],
};

const OPEN_POSITIONS = [
  { symbol: "BTC-USDT", side: "LONG", entry: 61240, mark: 62180, pnlPct: 1.53, pnlUsd: 94.2, sl: 60100, tp: 64500 },
  { symbol: "ETH-USDT", side: "SHORT", entry: 3180, mark: 3210, pnlPct: -0.94, pnlUsd: -28.4, sl: 3260, tp: 3050 },
  { symbol: "SOL-USDT", side: "LONG", entry: 142.3, mark: 148.9, pnlPct: 4.64, pnlUsd: 139.8, sl: 136.0, tp: 158.0 },
];

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

// Пока используем моки напрямую. Когда бэкенд будет готов — раскомментируйте
// вызовы apiGet(...) внутри компонентов ниже (см. TODO по коду).

const TRADE_HISTORY = [
  { symbol: "BTC-USDT", side: "LONG", date: "06 сен, 14:22", pnlUsd: 61.4, pnlPct: 1.02 },
  { symbol: "ARB-USDT", side: "SHORT", date: "06 сен, 09:05", pnlUsd: -18.9, pnlPct: -1.4 },
  { symbol: "SOL-USDT", side: "LONG", date: "05 сен, 22:47", pnlUsd: 44.3, pnlPct: 2.1 },
  { symbol: "ETH-USDT", side: "LONG", date: "05 сен, 11:30", pnlUsd: 12.7, pnlPct: 0.4 },
  { symbol: "BTC-USDT", side: "SHORT", date: "04 сен, 19:12", pnlUsd: -33.5, pnlPct: -1.9 },
];

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------

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
  const series = EQUITY_SERIES[period];
  const chartData = useMemo(() => series.map((v, i) => ({ i, v })), [series]);

  const first = series[0];
  const last = series[series.length - 1];
  const changeUsd = last - first;
  const changePct = (changeUsd / first) * 100;
  const up = changeUsd >= 0;

  return (
    <div className="tab-pane">
      {/* Hero */}
      <div className="hero">
        <div className="hero-top">
          <span className="hero-label">Общий баланс</span>
          <div className="period-pills">
            {Object.keys(EQUITY_SERIES).map((p) => (
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

        <div className="hero-figure">
          $
          {last.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </div>

        <div className={`hero-change ${up ? "text-profit" : "text-loss"}`}>
          {up ? <TrendingUp size={15} /> : <TrendingDown size={15} />}
          <span>{fmtUsd(changeUsd)}</span>
          <span className="hero-change-pct">({fmtPct(changePct)})</span>
          <span className="hero-change-period">за период</span>
        </div>

        <div className="chart-wrap">
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
                formatter={(v) => [`$${v.toLocaleString()}`, "Баланс"]}
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
        </div>
      </div>

      {/* Stat chips */}
      <div className="chip-row">
        <div className="chip">
          <span className="chip-label">Win rate</span>
          <span className="chip-value text-profit">63%</span>
        </div>
        <div className="chip">
          <span className="chip-label">Сделок</span>
          <span className="chip-value">128</span>
        </div>
        <div className="chip">
          <span className="chip-label">Просадка</span>
          <span className="chip-value text-loss">−8.4%</span>
        </div>
      </div>

      {/* Open positions */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">Открытые позиции</span>
          <span className="section-count">{OPEN_POSITIONS.length}</span>
        </div>
        <div className="list">
          {OPEN_POSITIONS.map((p) => (
            <div className="row" key={p.symbol}>
              <div className="row-main">
                <div className="row-title-line">
                  <span className="symbol">{p.symbol}</span>
                  <SideBadge side={p.side} />
                </div>
                <div className="row-sub">
                  вход {p.entry.toLocaleString()} · маркировка {p.mark.toLocaleString()}
                </div>
                <div className="row-sub row-sub-faint">
                  SL {p.sl.toLocaleString()} · TP {p.tp.toLocaleString()}
                </div>
              </div>
              <div className="row-end">
                <span className={`row-usd ${p.pnlUsd >= 0 ? "text-profit" : "text-loss"}`}>
                  {fmtUsd(p.pnlUsd)}
                </span>
                <PnlTag value={p.pnlUsd} pct={p.pnlPct} />
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Trade history */}
      <div className="section">
        <div className="section-head">
          <span className="section-title">История сделок</span>
        </div>
        <div className="list">
          {TRADE_HISTORY.map((t, idx) => (
            <div className="row row-compact" key={idx}>
              <div className="row-main">
                <div className="row-title-line">
                  <span className="symbol">{t.symbol}</span>
                  <SideBadge side={t.side} />
                </div>
                <div className="row-sub">{t.date}</div>
              </div>
              <div className="row-end">
                <span className={`row-usd ${t.pnlUsd >= 0 ? "text-profit" : "text-loss"}`}>
                  {fmtUsd(t.pnlUsd)}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Profile tab
// ---------------------------------------------------------------------------

function ProfileTab() {
  return (
    <div className="tab-pane">
      <div className="profile-head">
        <div className="avatar">ДМ</div>
        <div>
          <div className="profile-name">DMI Works</div>
          <div className="profile-handle">@dmi_trader</div>
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Подключение к бирже</span>
        </div>
        <div className="list">
          <div className="row">
            <div className="row-main">
              <div className="row-title-line">
                <span className="symbol">BingX</span>
                <span className="mode-badge mode-testnet">Testnet</span>
              </div>
              <div className="row-sub">API-ключ подключён · только торговля</div>
            </div>
            <ChevronRight size={16} className="chev" />
          </div>
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Баланс аккаунта</span>
        </div>
        <div className="list">
          <div className="kv-row">
            <span className="kv-label">Доступно</span>
            <span className="kv-value">$8,940.12</span>
          </div>
          <div className="kv-row">
            <span className="kv-label">В позициях</span>
            <span className="kv-value">$3,540.20</span>
          </div>
          <div className="kv-row">
            <span className="kv-label">Всего</span>
            <span className="kv-value kv-value-strong">$12,480.32</span>
          </div>
        </div>
      </div>

      <button className="danger-btn">
        <LogOut size={16} />
        Выйти из аккаунта
      </button>
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
