import { useCallback, useEffect, useState } from "react";
import { BarChart3, User, SettingsIcon, Coins, Microscope, Shield } from "lucide-react";
import StatisticsTab from "./components/stats/StatisticsTab";
import ProfileTab from "./components/profile/ProfileTab";
import SettingsTab from "./components/settings/SettingsTab";
import CoinsTab from "./components/coins/CoinsTab";
import AnalyticsTab from "./components/analytics/AnalyticsTab";
import AdminTab from "./components/admin/AdminTab";
import ConnectKeys from "./components/account/ConnectKeys";
import GuideScreen from "./components/guide/GuideScreen";
import Welcome from "./components/guide/Welcome";
import { GUIDE_EVENT } from "./components/guide/guideEvents";
import LanguagePicker from "./components/language/LanguagePicker";
import { Spinner } from "./components/common";
import { apiGet, apiPost } from "./lib/api";
import { setLang, t } from "./i18n";
import "./styles/global.css";

// label — ключ перевода (t), не текст: язык может смениться на лету
const ADMIN_TAB = { id: "admin", label: "tab_admin", icon: Shield, render: AdminTab };

const BASE_TABS = [
  { id: "stats", label: "tab_stats", icon: BarChart3, render: StatisticsTab },
  { id: "analytics", label: "tab_analytics", icon: Microscope, render: AnalyticsTab },
  { id: "coins", label: "tab_coins", icon: Coins, render: CoinsTab },
  { id: "profile", label: "tab_profile", icon: User, render: ProfileTab },
  { id: "settings", label: "tab_settings", icon: SettingsIcon, render: SettingsTab },
];

const REASON_KEYS = { blocked: "reason_blocked", unpaid: "reason_unpaid", user_not_found: "reason_user_not_found" };

export default function App() {
  const [active, setActive] = useState("stats");
  const [me, setMeRaw] = useState(null);
  const [error, setError] = useState(null);
  // язык применяем до рендера: t() читает его из модуля i18n
  const setMe = useCallback((m) => { setLang(m?.lang || "en"); setMeRaw(m); }, []);
  const tabs = me?.role === "admin" ? [...BASE_TABS, ADMIN_TAB] : BASE_TABS;
  const ActiveTab = tabs.find((tab) => tab.id === active).render;

  const loadMe = useCallback(() => {
    setError(null);
    return apiGet("/me").then(setMe).catch((e) => setError(e));
  }, [setMe]);

  // Выбор языка (первый вход или Профиль) сохраняется на бэкенде: им же
  // бот пишет уведомления в Telegram.
  const changeLang = useCallback(async (lang) => {
    setMe(await apiPost("/me/lang", { lang }));
  }, [setMe]);

  useEffect(() => { loadMe(); }, [loadMe]);

  // Інструкцію можна відкрити з будь-якого екрана (openGuide у guideEvents.js)
  const [guide, setGuide] = useState(null);
  useEffect(() => {
    const onOpen = (e) => setGuide({ section: e.detail?.section ?? null });
    window.addEventListener(GUIDE_EVENT, onOpen);
    return () => window.removeEventListener(GUIDE_EVENT, onOpen);
  }, []);
  const guideOverlay = guide && <GuideScreen section={guide.section} onClose={() => setGuide(null)} />;

  if (error) {
    const text = error.status === 401 ? t("err_open_via_bot") : t("err_load", { msg: error.message });
    return <div className="app-root"><div className="tab-pane"><div className="error-banner">{text}</div></div></div>;
  }
  if (!me) {
    return <div className="app-root"><div className="hero-loading"><Spinner /></div></div>;
  }
  // Первый запуск — сначала язык
  if (!me.lang) {
    return (
      <div className="app-root">
        <div className="tab-pane"><LanguagePicker onSelect={changeLang} /></div>
      </div>
    );
  }
  // Ключи не подключены (или сессия не запустилась) — сначала подключение биржи
  if (!me.session_active) {
    return (
      <div className="app-root" key={me.lang}>
        <div className="tab-pane">
          <Welcome />
          <ConnectKeys onDone={setMe} />
        </div>
        {guideOverlay}
      </div>
    );
  }

  return (
    // key={me.lang}: при смене языка дерево перемонтируется и все тексты перерисуются
    <div className="app-root" key={me.lang}>
      {!me.can_open && (
        <div className="error-banner">
          {t("banner_stopped", { reason: REASON_KEYS[me.can_open_reason] ? t(REASON_KEYS[me.can_open_reason]) : me.can_open_reason })}
        </div>
      )}
      <ActiveTab me={me} onChangeLang={changeLang} />
      {guideOverlay}

      <nav className="tabbar">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={`tabbar-btn ${active === tab.id ? "active" : ""}`}
              onClick={() => setActive(tab.id)}
            >
              <Icon size={20} strokeWidth={active === tab.id ? 2.3 : 1.8} />
              <span>{t(tab.label)}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}