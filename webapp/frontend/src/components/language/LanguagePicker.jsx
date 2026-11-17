import { useState } from "react";
import { Globe, Check } from "lucide-react";
import { LANGS, detectTelegramLang } from "../../i18n";
import { Spinner } from "../common";

const ordered = (preferred) =>
  preferred ? [...LANGS].sort((a, b) => (b.code === preferred) - (a.code === preferred)) : LANGS;

// Список языков: на первом входе (fullscreen) и в Профиле (current = выбранный).
export function LanguageList({ current, onSelect, preferred }) {
  const [busy, setBusy] = useState(null);
  const pick = async (code) => {
    if (busy || code === current) return;
    setBusy(code);
    try { await onSelect(code); } finally { setBusy(null); }
  };
  return (
    <div className="list">
      {ordered(preferred).map((l) => (
        <div className="row row-clickable" key={l.code} onClick={() => pick(l.code)}>
          <div className="row-icon lang-flag">{l.flag}</div>
          <div className="row-main"><span className="settings-title">{l.name}</span></div>
          {busy === l.code ? <Spinner /> : l.code === current && <Check size={16} className="text-profit" />}
        </div>
      ))}
    </div>
  );
}

// Первый запуск: язык ещё не выбран. Заголовок — на всех трёх языках
// (язык Telegram-клиента первым), его же вариант — первый в списке.
const TITLES = { en: "Choose your language", ru: "Выберите язык", uk: "Оберіть мову" };
export default function LanguagePicker({ onSelect }) {
  const detected = detectTelegramLang();
  return (
    <div className="welcome">
      <div className="welcome-hero">
        <div className="welcome-glow" />
        <div className="guide-hero-icon"><Globe size={30} /></div>
        <h1 className="welcome-title">{TITLES[detected]}</h1>
        <p className="welcome-text">
          {Object.keys(TITLES).filter((c) => c !== detected).map((c) => TITLES[c]).join(" · ")}
        </p>
      </div>
      <div className="section">
        <LanguageList current={null} onSelect={onSelect} preferred={detected} />
      </div>
    </div>
  );
}
