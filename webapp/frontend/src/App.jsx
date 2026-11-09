import { useCallback, useEffect, useState } from "react";
import { BarChart3, User, SettingsIcon, Coins, Microscope, Shield } from "lucide-react";
import StatisticsTab from "./components/stats/StatisticsTab";
import ProfileTab from "./components/profile/ProfileTab";
import SettingsTab from "./components/settings/SettingsTab";
import CoinsTab from "./components/coins/CoinsTab";
import AnalyticsTab from "./components/analytics/AnalyticsTab";
import AdminTab from "./components/admin/AdminTab";
import ConnectKeys from "./components/account/ConnectKeys";
import { Spinner } from "./components/common";
import { apiGet } from "./lib/api";
import "./styles/global.css";

const ADMIN_TAB = { id: "admin", label: "Админ", icon: Shield, render: AdminTab };

const BASE_TABS = [
  { id: "stats", label: "Статистика", icon: BarChart3, render: StatisticsTab },
  { id: "analytics", label: "Аналитика", icon: Microscope, render: AnalyticsTab },
  { id: "coins", label: "Монеты", icon: Coins, render: CoinsTab },
  { id: "profile", label: "Профиль", icon: User, render: ProfileTab },
  { id: "settings", label: "Настройки", icon: SettingsIcon, render: SettingsTab },
];

export default function App() {
  const [active, setActive] = useState("stats");
  const [me, setMe] = useState(null);
  const [error, setError] = useState(null);
  const tabs = me?.role === "admin" ? [...BASE_TABS, ADMIN_TAB] : BASE_TABS;
  const ActiveTab = tabs.find((t) => t.id === active).render;

  const loadMe = useCallback(() => {
    setError(null);
    return apiGet("/me").then(setMe).catch((e) => setError(e));
  }, []);

  useEffect(() => { loadMe(); }, [loadMe]);

  if (error) {
    const text = error.status === 401
      ? "Откройте приложение через бота в Telegram."
      : `Не удалось загрузить данные: ${error.message}`;
    return <div className="app-root"><div className="tab-pane"><div className="error-banner">{text}</div></div></div>;
  }
  if (!me) {
    return <div className="app-root"><div className="hero-loading"><Spinner /></div></div>;
  }
  // Ключи не подключены (или сессия не запустилась) — сначала подключение биржи
  if (!me.session_active) {
    return (
      <div className="app-root">
        <div className="tab-pane">
          <ConnectKeys onDone={setMe} />
        </div>
      </div>
    );
  }

  return (
    <div className="app-root">
      {!me.can_open && (
        <div className="error-banner">Новые сделки остановлены: {me.can_open_reason}. Открытые позиции сопровождаются.</div>
      )}
      <ActiveTab />

      <nav className="tabbar">
        {tabs.map((t) => {
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