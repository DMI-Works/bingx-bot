import { useState } from "react";
import { BarChart3, User, SettingsIcon } from "lucide-react";
import StatisticsTab from "./components/stats/StatisticsTab";
import ProfileTab from "./components/profile/ProfileTab";
import SettingsTab from "./components/settings/SettingsTab";
import "./styles/global.css";

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
